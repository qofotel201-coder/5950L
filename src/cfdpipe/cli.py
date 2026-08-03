"""Command-line entry points for the CFD program connection layer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tomllib
import traceback
from typing import Any, Mapping
from uuid import uuid4

from . import __version__
from .bridges import (
    GmshBridge,
    GmshBridgeError,
    ParaViewBridge,
    ParaViewBridgeError,
    SU2Bridge,
    SU2BridgeError,
)
from .boundary_layer_trial import (
    BoundaryLayerTrialError,
    build_boundary_layer_trial,
    calculate_boundary_layer_schedule,
    normalize_boundary_layer_trial_config,
)
from .boundary_layer_smoke import (
    BoundaryLayerSmokeError,
    RealProjectBoundaryLayerStrategy,
    build_boundary_layer_smoke,
    normalize_boundary_layer_smoke_config,
)
from .boundary_layer_clearance import (
    BoundaryLayerClearanceError,
    run_boundary_layer_clearance_command,
)
from .boundary_layer_local_schedule import (
    BoundaryLayerLocalScheduleError,
    run_boundary_layer_local_schedule_command,
)
from .boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
    load_and_bind_boundary_layer_local_schedule,
)
from .coarse_calibration_gate import (
    REPORT_SCHEMA as COARSE_CALIBRATION_PREFLIGHT_SCHEMA,
    CoarseCalibrationGateError,
    capture_system_snapshot,
    evaluate_first_point_preflight,
    evaluate_second_point_preflight,
)
from .coarse_projection_gate import evaluate_projection_preflight
from .coarse_projection_evidence import (
    CoarseProjectionEvidenceError,
    load_and_validate_projection_evidence,
    validate_projection_evidence_record,
)
from .coarse_mesh import CoarseMeshError, normalize_coarse_mesh_config
from .coarse_repair_audit import (
    CoarseRepairAuditError,
    PHYSICAL_HOMOTOPY_PURPOSE,
    PHYSICAL_HOMOTOPY_REQUEST,
    RESOURCE_ABORT_MANIFEST_NAME,
    SCHEDULE_CONTINUATION_PURPOSE,
    _physical_surface_schedule_values,
    load_and_validate_physical_homotopy_audit_manifest,
    make_coarse_repair_audit_strategy_config,
    publish_coarse_repair_resource_abort_manifest,
    validate_coarse_repair_discovery,
)
from .coarse_schedule_continuation import (
    CoarseScheduleContinuationError,
    DISCOVERY_SCHEMA as SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA,
    REQUEST as SCHEDULE_CONTINUATION_REQUEST,
    make_schedule_frontier_direction_endpoint,
    validate_schedule_frontier_direction_endpoint,
    validate_schedule_frontier_direction_continuation,
)
from .coarse_schedule_feasibility import (
    CoarseScheduleFeasibilityError,
    validate_minimum_growth_application,
    validate_minimum_growth_endpoint,
)
from .coarse_direction_feasibility import (
    CoarseDirectionFeasibilityError,
    ENDPOINT_SCHEMA as OWNER_FREE_DIRECTION_ENDPOINT_SCHEMA,
    PATTERN_ENDPOINT_SCHEMA as OWNER_FREE_PATTERN_ENDPOINT_SCHEMA,
    validate_owner_free_direction_endpoint,
)
from .coarse_component_direction import (
    CoarseComponentDirectionError,
    validate_component_direction_discovery,
)
from .coarse_direction_replay import (
    CoarseDirectionReplayError,
    HOMOTOPY_DISCOVERY_SCHEMA,
    PHYSICAL_DISCOVERY_SCHEMA,
    load_direction_replay_consensus_approval,
    make_physical_schedule_homotopy_endpoint,
    validate_direction_replay_approval,
    validate_physical_schedule_homotopy_discovery,
    validate_physical_schedule_homotopy_endpoint,
    validate_physical_schedule_replay_discovery,
)
from .pipeline import (
    ConnectionTestError,
    Pipeline,
    PipelineStage,
    PipelineStageError,
    RealConnectionError,
    load_real_connection_inputs,
    run_connect_test,
    run_real_connection,
    select_real_case,
)
from .geometry_review_pipeline import (
    GeometryEvidenceReviewError,
    run_geometry_evidence_review,
)
from .geometry_repair import GeometryRepairError, repair_shared_topology
from .interface_persistence import (
    InterfacePersistenceError,
    validate_interface_persistence,
)
from .marker_config import MarkerConfigError, write_marker_config
from .outlet_validation_pipeline import (
    OutletValidationPipelineError,
    run_outlet_validation,
)
from .rans_smoke_pipeline import RANSSmokePipelineError, run_rans_smoke_validation
from .rear_outlet_freeze import evaluate_rear_outlet_freeze
from .process import CommandExecutionError, CommandRunner
from .toolchain import ENV_VARS, TOOL_NAMES, Toolchain, ToolchainError
from .worker_memory import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    LIMIT_REPORT_SCHEMA,
    PRIVATE_COMMIT_CURRENT_SOURCE,
    PRIVATE_COMMIT_PEAK_SOURCE,
    PRIVATE_COMMIT_SEMANTICS_SCHEMA,
    PROCESS_REPORT_SCHEMA,
    SYSTEM_REPORT_SCHEMA,
    capture_system_physical_memory,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS_CONFIG = REPOSITORY_ROOT / "config" / "tools.json"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "runs" / "connection"
DEFAULT_GMSH_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / "gmsh"
DEFAULT_SU2_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / "su2"
DEFAULT_PARAVIEW_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / "paraview"
DEFAULT_PARAVIEW_SCRIPTS = REPOSITORY_ROOT / "scripts" / "paraview"
DEFAULT_REAL_CONNECTION_OUTPUT_DIR = REPOSITORY_ROOT / "runs" / "real_connection"
DEFAULT_OUTLET_VALIDATION_OUTPUT_DIR = (
    REPOSITORY_ROOT / "runs" / "outlet_validation" / "physical_gate"
)
DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR = (
    DEFAULT_REAL_CONNECTION_OUTPUT_DIR / "geometry_inspection"
)
DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR = (
    DEFAULT_REAL_CONNECTION_OUTPUT_DIR / "geometry_review"
)
DEFAULT_GEOMETRY_REPAIR_OUTPUT_DIR = (
    DEFAULT_REAL_CONNECTION_OUTPUT_DIR / "geometry_repair"
)
DEFAULT_GEOMETRY_REPAIR_VALIDATION_OUTPUT_DIR = (
    DEFAULT_REAL_CONNECTION_OUTPUT_DIR / "geometry_repair_validation"
)
DEFAULT_DERIVED_GEOMETRY_DIR = (
    REPOSITORY_ROOT / "geometry" / "derived" / "model1_shared_topology"
)
DEFAULT_MARKERS_CONFIG = REPOSITORY_ROOT / "config" / "markers.toml"
DEFAULT_TOPOLOGY_SMOKE_CONFIG = REPOSITORY_ROOT / "config" / "topology_smoke.toml"
DEFAULT_BOUNDARY_LAYER_TRIAL_CONFIG = (
    REPOSITORY_ROOT / "config" / "boundary_layer_trial.toml"
)
DEFAULT_BOUNDARY_LAYER_TRIAL_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "runs" / "boundary_layer_trial"
)
DEFAULT_BOUNDARY_LAYER_TRIAL_OUTPUT_DIR = (
    DEFAULT_BOUNDARY_LAYER_TRIAL_OUTPUT_ROOT / "qualified_patch_1"
)
DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG = (
    REPOSITORY_ROOT / "config" / "boundary_layer_smoke.toml"
)
DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "runs" / "boundary_layer_smoke"
)
DEFAULT_BOUNDARY_LAYER_QUALITY_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "runs" / "boundary_layer_quality"
)
DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_DIR = (
    DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT / "implicit_baseline_1"
)
DEFAULT_RANS_SMOKE_CONFIG = REPOSITORY_ROOT / "config" / "rans_smoke.toml"
DEFAULT_REAR_OUTLET_PILOT_CONFIG = (
    REPOSITORY_ROOT / "config" / "rear_outlet_pilot.toml"
)
DEFAULT_RANS_SMOKE_OUTPUT_ROOT = REPOSITORY_ROOT / "runs" / "rans_smoke"
DEFAULT_REAR_OUTLET_FREEZE_CONFIG = (
    REPOSITORY_ROOT / "config" / "rear_outlet_freeze.toml"
)
DEFAULT_REAR_OUTLET_FREEZE_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "runs" / "rear_outlet_freeze"
)
DEFAULT_COARSE_MESH_CONFIG = REPOSITORY_ROOT / "config" / "coarse_mesh.toml"
DEFAULT_COARSE_MESH_OUTPUT_ROOT = REPOSITORY_ROOT / "runs" / "mesh" / "coarse"
DEFAULT_RANS_MESH_MANIFEST = (
    REPOSITORY_ROOT
    / "runs"
    / "boundary_layer_quality"
    / "quality_gate_20260802_001"
    / "boundary_layer_smoke_manifest.json"
)
DEFAULT_RANS_OUTLET_EVIDENCE = (
    REPOSITORY_ROOT
    / "runs"
    / "outlet_validation"
    / "physical_gate_retry1"
    / "outlet_validation_manifest.json"
)
DEFAULT_MARKER_CONFIG_EVIDENCE_DIR = (
    DEFAULT_REAL_CONNECTION_OUTPUT_DIR / "marker_config"
)


def _tool_option(name: str) -> str:
    return "--" + name.replace("_", "-")


def _add_runtime_options(
    parser: argparse.ArgumentParser,
    *,
    tools: tuple[str, ...] = (),
    include_tool_config: bool = True,
    include_process_options: bool = True,
) -> None:
    if include_tool_config:
        parser.add_argument(
            "--tools-config",
            type=Path,
            default=DEFAULT_TOOLS_CONFIG,
            help="tool path configuration JSON",
        )
    parser.set_defaults(output_dir=DEFAULT_OUTPUT_DIR)
    if include_process_options:
        parser.add_argument("--timeout", type=float, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="disable live mirroring while retaining stdout/stderr log files",
        )
    for tool in tools:
        parser.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cfdpipe", description=__doc__)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    top = parser.add_subparsers(dest="command", required=True)

    tools_parser = top.add_parser("tools", help="inspect configured external tools")
    tools_sub = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_check = tools_sub.add_parser("check", help="resolve and validate tool paths")
    _add_runtime_options(
        tools_check,
        tools=TOOL_NAMES,
        include_process_options=False,
    )
    tools_check.set_defaults(handler=_handle_tools_check)

    gmsh_parser = top.add_parser("gmsh", help="Gmsh Python API connection commands")
    gmsh_sub = gmsh_parser.add_subparsers(dest="gmsh_command", required=True)
    gmsh_smoke = gmsh_sub.add_parser(
        "smoke", help="generate and validate the two-dimensional Gmsh smoke mesh"
    )
    gmsh_smoke.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GMSH_OUTPUT_DIR,
        help="output directory under runs/connection",
    )
    gmsh_smoke.add_argument(
        "--tools-config",
        type=Path,
        default=DEFAULT_TOOLS_CONFIG,
        help="tool path configuration JSON",
    )
    gmsh_smoke.add_argument(
        "--gmsh",
        type=Path,
        default=None,
        help=f"override Gmsh CLI path (takes priority over {ENV_VARS['gmsh']})",
    )
    gmsh_smoke.add_argument("--timeout", type=float, default=10.0)
    gmsh_smoke.add_argument("--quiet", action="store_true")
    gmsh_smoke.set_defaults(output_dir=DEFAULT_GMSH_OUTPUT_DIR)
    gmsh_smoke.set_defaults(handler=_handle_gmsh_smoke)
    gmsh_catalog = gmsh_sub.add_parser(
        "catalog", help="inspect one repository STEP with the Gmsh Python API"
    )
    gmsh_catalog.add_argument("--step", type=Path, required=True)
    gmsh_catalog.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR,
        help="must be the repository runs/real_connection/geometry_inspection directory",
    )
    gmsh_catalog.set_defaults(handler=_handle_gmsh_catalog)
    gmsh_review = gmsh_sub.add_parser(
        "review",
        help="collect bounded STEP topology, surface-mesh and pvbatch evidence",
    )
    gmsh_review.add_argument("--step", type=Path, required=True)
    gmsh_review.add_argument(
        "--project", type=Path, default=REPOSITORY_ROOT / "config" / "project.toml"
    )
    gmsh_review.add_argument(
        "--output", type=Path, default=DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR
    )
    gmsh_review.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    gmsh_review.add_argument("--pvbatch", type=Path, default=None)
    gmsh_review.add_argument("--mesh-size", type=float, default=0.20)
    gmsh_review.add_argument("--max-triangles", type=int, default=100_000)
    gmsh_review.add_argument("--timeout", type=float, default=300.0)
    gmsh_review.add_argument("--quiet", action="store_true")
    gmsh_review.set_defaults(handler=_handle_gmsh_review)
    gmsh_repair = gmsh_sub.add_parser(
        "repair",
        help="make the two proven coincident interfaces conformal without meshing",
    )
    gmsh_repair.add_argument(
        "--step",
        type=Path,
        required=True,
        help="canonical read-only geometry/raw/model1.step",
    )
    gmsh_repair.set_defaults(handler=_handle_gmsh_repair)
    gmsh_validate_repair = gmsh_sub.add_parser(
        "validate-repair",
        help="independently prove source-interface persistence in the derived BREP",
    )
    gmsh_validate_repair.add_argument(
        "--step",
        type=Path,
        required=True,
        help="canonical read-only geometry/raw/model1.step",
    )
    gmsh_validate_repair.add_argument(
        "--brep",
        type=Path,
        default=DEFAULT_DERIVED_GEOMETRY_DIR / "model1_shared_topology.brep",
        help="read-only shared-topology pipeline BREP",
    )
    gmsh_validate_repair.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GEOMETRY_REPAIR_VALIDATION_OUTPUT_DIR,
        help="must be runs/real_connection/geometry_repair_validation",
    )
    gmsh_validate_repair.set_defaults(handler=_handle_gmsh_validate_repair)
    gmsh_markers = gmsh_sub.add_parser(
        "markers",
        help="materialize the proven tag-free marker contract without meshing",
    )
    gmsh_markers.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MARKERS_CONFIG,
        help="must be the repository config/markers.toml path",
    )
    gmsh_markers.set_defaults(handler=_handle_gmsh_markers)
    gmsh_step = gmsh_sub.add_parser(
        "step", help="configured STEP API entry point (requires external markers)"
    )
    gmsh_step.add_argument("step_file", type=Path)
    gmsh_step.set_defaults(handler=_handle_gmsh_step)

    su2_parser = top.add_parser("su2", help="SU2 subprocess connection commands")
    su2_sub = su2_parser.add_subparsers(dest="su2_command", required=True)
    su2_smoke = su2_sub.add_parser(
        "smoke", help="run the five-step Euler SU2 connection case"
    )
    su2_smoke.add_argument("--mesh", type=Path, required=True)
    su2_smoke.add_argument(
        "--output", type=Path, default=DEFAULT_SU2_OUTPUT_DIR
    )
    su2_smoke.add_argument("--nproc", type=int, default=1)
    su2_smoke.add_argument("--max-iterations", type=int, default=5)
    su2_smoke.add_argument("--timeout", type=float, default=300.0)
    su2_smoke.add_argument("--quiet", action="store_true")
    su2_smoke.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    for tool in ("su2_cfd", "su2_sol", "mpiexec"):
        su2_smoke.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )
    su2_smoke.set_defaults(output_dir=DEFAULT_SU2_OUTPUT_DIR)
    su2_smoke.set_defaults(handler=_handle_su2_smoke)
    su2_run = su2_sub.add_parser("run", help="run one explicitly supplied SU2 cfg")
    su2_run.add_argument("config_file", type=Path)
    su2_run.add_argument("--nproc", type=int, default=1)
    _add_runtime_options(su2_run, tools=("su2_cfd", "mpiexec"))
    su2_run.set_defaults(handler=_handle_su2_run)

    paraview_parser = top.add_parser(
        "paraview", help="headless ParaView pvbatch connection commands"
    )
    paraview_sub = paraview_parser.add_subparsers(
        dest="paraview_command", required=True
    )
    paraview_inspect = paraview_sub.add_parser(
        "inspect", help="inspect the SU2-manifest-selected solution with pvbatch"
    )
    paraview_inspect.add_argument("--manifest", type=Path, required=True)
    paraview_inspect.add_argument(
        "--output", type=Path, default=DEFAULT_PARAVIEW_OUTPUT_DIR
    )
    paraview_inspect.add_argument("--timeout", type=float, default=300.0)
    paraview_inspect.add_argument("--quiet", action="store_true")
    paraview_inspect.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    paraview_inspect.add_argument(
        "--pvbatch",
        type=Path,
        default=None,
        help=f"override pvbatch path (takes priority over {ENV_VARS['pvbatch']})",
    )
    paraview_inspect.set_defaults(output_dir=DEFAULT_PARAVIEW_OUTPUT_DIR)
    paraview_inspect.set_defaults(handler=_handle_paraview_inspect)

    connect_test = top.add_parser(
        "connect-test", help="run the real small fail-fast end-to-end connection case"
    )
    connect_test.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="must resolve to the repository runs/connection directory",
    )
    connect_test.add_argument(
        "--clean",
        action="store_true",
        help="remove only verified connection-test-owned artifacts before running",
    )
    connect_test.add_argument("--nproc", type=int, default=1)
    connect_test.add_argument("--timeout", type=float, default=300.0)
    connect_test.add_argument("--quiet", action="store_true")
    connect_test.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    for tool in TOOL_NAMES:
        connect_test.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )
    connect_test.set_defaults(output_dir=DEFAULT_OUTPUT_DIR)
    connect_test.set_defaults(handler=_handle_connect_test)

    pipeline_parser = top.add_parser("pipeline", help="pipeline orchestration commands")
    pipeline_sub = pipeline_parser.add_subparsers(
        dest="pipeline_command", required=True
    )
    pipeline_run = pipeline_sub.add_parser(
        "run", help="run the real-input, connection-scale fail-fast pipeline"
    )
    pipeline_run.add_argument("--step", type=Path, required=True)
    pipeline_run.add_argument("--project", type=Path, required=True)
    pipeline_run.add_argument("--cases", type=Path, required=True)
    pipeline_run.add_argument("--markers", type=Path, required=True)
    pipeline_run.add_argument(
        "--topology-smoke-config",
        type=Path,
        default=DEFAULT_TOPOLOGY_SMOKE_CONFIG,
        help="stable-fingerprint local refinement configuration",
    )
    pipeline_run.add_argument("--case", dest="case_id", required=True)
    pipeline_run.add_argument("--mesh-level", choices=("smoke",), required=True)
    pipeline_run.add_argument("--nproc", type=int, default=1)
    pipeline_run.add_argument("--max-iterations", type=int, default=5)
    pipeline_run.add_argument(
        "--smoke-characteristic-length",
        type=float,
        default=0.25,
        help="topology-smoke global maximum tetrahedral size in metres",
    )
    pipeline_run.add_argument(
        "--facet-overlap-angle-tolerance",
        type=float,
        default=0.1,
        help=(
            "Gmsh sharp-facet overlap threshold in degrees; may only tighten "
            "the 0.1 degree default"
        ),
    )
    pipeline_run.add_argument(
        "--gmsh-algorithm-3d",
        type=int,
        choices=(1, 4, 10),
        default=1,
        help="Gmsh 3D algorithm: 1 Delaunay, 4 Frontal, 10 HXT",
    )
    pipeline_run.add_argument("--timeout", type=float, default=300.0)
    pipeline_run.add_argument("--quiet", action="store_true")
    pipeline_run.add_argument(
        "--output", type=Path, default=DEFAULT_REAL_CONNECTION_OUTPUT_DIR
    )
    pipeline_run.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    for tool in TOOL_NAMES:
        pipeline_run.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )
    pipeline_run.set_defaults(handler=_handle_pipeline_run)

    outlet_check = pipeline_sub.add_parser(
        "outlet-check",
        help="run the bounded coordinate/atmosphere/rear-outlet evidence gate",
    )
    outlet_check.add_argument("--step", type=Path, required=True)
    outlet_check.add_argument("--project", type=Path, required=True)
    outlet_check.add_argument("--cases", type=Path, required=True)
    outlet_check.add_argument("--markers", type=Path, required=True)
    outlet_check.add_argument(
        "--topology-smoke-config",
        type=Path,
        default=DEFAULT_TOPOLOGY_SMOKE_CONFIG,
    )
    outlet_check.add_argument("--case", dest="case_id", required=True)
    outlet_check.add_argument("--mesh", type=Path, required=True)
    outlet_check.add_argument("--mesh-manifest", type=Path, required=True)
    outlet_check.add_argument("--max-iterations", type=int, default=200)
    outlet_check.add_argument("--timeout", type=float, default=300.0)
    outlet_check.add_argument("--quiet", action="store_true")
    outlet_check.add_argument(
        "--output", type=Path, default=DEFAULT_OUTLET_VALIDATION_OUTPUT_DIR
    )
    outlet_check.add_argument(
        "--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG
    )
    for tool in ("su2_cfd", "pvbatch"):
        outlet_check.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )
    outlet_check.set_defaults(handler=_handle_pipeline_outlet_check)

    boundary_layer_trial = pipeline_sub.add_parser(
        "boundary-layer-trial",
        help="generate and audit a local prism-only layer patch without a solver",
    )
    boundary_layer_trial.add_argument("--step", type=Path, required=True)
    boundary_layer_trial.add_argument("--project", type=Path, required=True)
    boundary_layer_trial.add_argument("--cases", type=Path, required=True)
    boundary_layer_trial.add_argument("--markers", type=Path, required=True)
    boundary_layer_trial.add_argument(
        "--topology-smoke-config",
        type=Path,
        default=DEFAULT_TOPOLOGY_SMOKE_CONFIG,
        help="existing hash-bound real-project input gate",
    )
    boundary_layer_trial.add_argument(
        "--trial-config",
        type=Path,
        default=DEFAULT_BOUNDARY_LAYER_TRIAL_CONFIG,
    )
    boundary_layer_trial.add_argument("--case", dest="case_id", required=True)
    boundary_layer_trial.add_argument(
        "--output", type=Path, default=DEFAULT_BOUNDARY_LAYER_TRIAL_OUTPUT_DIR
    )
    boundary_layer_trial.set_defaults(handler=_handle_pipeline_boundary_layer_trial)

    boundary_layer_smoke = pipeline_sub.add_parser(
        "boundary-layer-smoke",
        help="generate and audit a complete small mixed boundary-layer mesh",
    )
    boundary_layer_smoke.add_argument("--step", type=Path, required=True)
    boundary_layer_smoke.add_argument("--project", type=Path, required=True)
    boundary_layer_smoke.add_argument("--cases", type=Path, required=True)
    boundary_layer_smoke.add_argument("--markers", type=Path, required=True)
    boundary_layer_smoke.add_argument(
        "--topology-smoke-config",
        type=Path,
        default=DEFAULT_TOPOLOGY_SMOKE_CONFIG,
    )
    boundary_layer_smoke.add_argument(
        "--smoke-config",
        type=Path,
        default=DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG,
    )
    boundary_layer_smoke.add_argument(
        "--schedule-config",
        type=Path,
        default=DEFAULT_BOUNDARY_LAYER_TRIAL_CONFIG,
        help="existing design-case y+ planning schedule contract",
    )
    boundary_layer_smoke.add_argument("--case", dest="case_id", required=True)
    boundary_layer_smoke.add_argument(
        "--output", type=Path, default=DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_DIR
    )
    boundary_layer_smoke.set_defaults(handler=_handle_pipeline_boundary_layer_smoke)

    boundary_layer_clearance = pipeline_sub.add_parser(
        "boundary-layer-clearance",
        help="audit sampled wall-normal layer-stack clearance without meshing",
    )
    boundary_layer_clearance.add_argument(
        "--config", type=Path, default=DEFAULT_COARSE_MESH_CONFIG
    )
    boundary_layer_clearance.add_argument(
        "--markers", type=Path, default=DEFAULT_MARKERS_CONFIG
    )
    boundary_layer_clearance.add_argument("--output", type=Path, required=True)
    boundary_layer_clearance.set_defaults(
        handler=_handle_pipeline_boundary_layer_clearance
    )

    boundary_layer_local_plan = pipeline_sub.add_parser(
        "boundary-layer-local-plan",
        help="derive clearance-aware local prism schedules without meshing",
    )
    boundary_layer_local_plan.add_argument(
        "--config", type=Path, default=DEFAULT_COARSE_MESH_CONFIG
    )
    boundary_layer_local_plan.add_argument(
        "--clearance-report", type=Path, required=True
    )
    boundary_layer_local_plan.add_argument(
        "--clearance-sha256", required=True
    )
    boundary_layer_local_plan.add_argument("--output", type=Path, required=True)
    boundary_layer_local_plan.set_defaults(
        handler=_handle_pipeline_boundary_layer_local_plan
    )

    coarse_mesh_project = pipeline_sub.add_parser(
        "coarse-mesh-project",
        help=(
            "count a temporary one-layer real-geometry mesh in a hard-limited "
            "worker without writing a mesh"
        ),
    )
    coarse_mesh_project.add_argument(
        "--config", type=Path, default=DEFAULT_COARSE_MESH_CONFIG
    )
    coarse_mesh_project.add_argument(
        "--characteristic-length", type=float, required=True
    )
    coarse_mesh_project.add_argument("--output", type=Path, required=True)
    coarse_mesh_project.add_argument("--timeout", type=float, default=300.0)
    coarse_mesh_project.add_argument("--quiet", action="store_true")
    coarse_mesh_project.set_defaults(handler=_handle_pipeline_coarse_mesh_project)

    coarse_repair_audit = pipeline_sub.add_parser(
        "coarse-repair-audit",
        help=(
            "discover stable coarse-mesh repair evidence in a hard-limited "
            "worker without writing a mesh"
        ),
    )
    coarse_repair_audit.add_argument(
        "--config", type=Path, default=DEFAULT_COARSE_MESH_CONFIG
    )
    coarse_repair_audit.add_argument(
        "--characteristic-length", type=float, required=True
    )
    coarse_repair_audit.add_argument(
        "--projection-manifest", type=Path, required=True
    )
    coarse_repair_audit.add_argument("--projection-sha256", required=True)
    coarse_repair_audit.add_argument(
        "--local-schedule", type=Path, required=True
    )
    coarse_repair_audit.add_argument("--local-schedule-sha256", required=True)
    coarse_repair_audit.add_argument(
        "--schedule-feasibility-endpoint",
        choices=(
            "minimum-growth-existing-direction-repair",
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
            PHYSICAL_HOMOTOPY_REQUEST,
            SCHEDULE_CONTINUATION_REQUEST,
        ),
    )
    coarse_repair_audit.add_argument(
        "--direction-audit-primary",
        type=Path,
        help="first independent PASS pattern-audit manifest",
    )
    coarse_repair_audit.add_argument(
        "--direction-audit-primary-sha256"
    )
    coarse_repair_audit.add_argument(
        "--direction-audit-confirmation",
        type=Path,
        help="second independent PASS pattern-audit manifest",
    )
    coarse_repair_audit.add_argument(
        "--direction-audit-confirmation-sha256"
    )
    coarse_repair_audit.add_argument(
        "--homotopy-audit-manifest",
        type=Path,
        help="validated INCOMPLETE fixed-direction homotopy audit manifest",
    )
    coarse_repair_audit.add_argument(
        "--homotopy-audit-manifest-sha256"
    )
    coarse_repair_audit.add_argument("--output", type=Path, required=True)
    coarse_repair_audit.add_argument("--timeout", type=float, default=840.0)
    coarse_repair_audit.add_argument("--quiet", action="store_true")
    coarse_repair_audit.set_defaults(handler=_handle_pipeline_coarse_repair_audit)

    coarse_mesh_calibrate = pipeline_sub.add_parser(
        "coarse-mesh-calibrate",
        help="run one bounded coarse-mesh calibration in an isolated Python worker",
    )
    coarse_mesh_calibrate.add_argument(
        "--config", type=Path, default=DEFAULT_COARSE_MESH_CONFIG
    )
    coarse_mesh_calibrate.add_argument(
        "--characteristic-length", type=float, required=True
    )
    coarse_mesh_calibrate.add_argument(
        "--first-manifest",
        type=Path,
        help="explicit PASS manifest from the first configured calibration point",
    )
    coarse_mesh_calibrate.add_argument(
        "--projection-manifest",
        type=Path,
        required=True,
        help="explicit PASS projection for the first configured calibration point",
    )
    coarse_mesh_calibrate.add_argument(
        "--projection-sha256",
        required=True,
        help="expected SHA-256 of --projection-manifest",
    )
    coarse_mesh_calibrate.add_argument(
        "--local-schedule",
        type=Path,
        required=True,
        help="explicit PASS clearance-aware local schedule JSON",
    )
    coarse_mesh_calibrate.add_argument(
        "--local-schedule-sha256",
        required=True,
        help="expected SHA-256 of --local-schedule",
    )
    coarse_mesh_calibrate.add_argument("--output", type=Path, required=True)
    coarse_mesh_calibrate.add_argument("--timeout", type=float, default=840.0)
    coarse_mesh_calibrate.add_argument("--quiet", action="store_true")
    coarse_mesh_calibrate.set_defaults(
        handler=_handle_pipeline_coarse_mesh_calibrate
    )

    rans_smoke = pipeline_sub.add_parser(
        "rans-smoke",
        help="run a bounded serial first-order SST RANS and y+ quality gate",
    )
    rans_smoke.add_argument("--step", type=Path, required=True)
    rans_smoke.add_argument("--project", type=Path, required=True)
    rans_smoke.add_argument("--cases", type=Path, required=True)
    rans_smoke.add_argument("--markers", type=Path, required=True)
    rans_smoke.add_argument(
        "--topology-smoke-config", type=Path, default=DEFAULT_TOPOLOGY_SMOKE_CONFIG
    )
    rans_smoke.add_argument(
        "--boundary-layer-trial-config",
        type=Path,
        default=DEFAULT_BOUNDARY_LAYER_TRIAL_CONFIG,
    )
    rans_smoke.add_argument(
        "--boundary-layer-smoke-config",
        type=Path,
        default=DEFAULT_BOUNDARY_LAYER_SMOKE_CONFIG,
    )
    rans_smoke.add_argument(
        "--rear-outlet-pilot-config",
        type=Path,
        default=DEFAULT_REAR_OUTLET_PILOT_CONFIG,
    )
    rans_smoke.add_argument(
        "--rans-config", type=Path, default=DEFAULT_RANS_SMOKE_CONFIG
    )
    rans_smoke.add_argument(
        "--mesh-manifest", type=Path, default=DEFAULT_RANS_MESH_MANIFEST
    )
    rans_smoke.add_argument(
        "--outlet-evidence", type=Path, default=DEFAULT_RANS_OUTLET_EVIDENCE
    )
    rans_smoke.add_argument("--case", dest="case_id", required=True)
    rans_smoke.add_argument("--nproc", type=int, default=1)
    rans_smoke.add_argument(
        "--spatial-order",
        type=int,
        choices=(1, 2),
        default=1,
        help="1 for a fresh startup; 2 requires --restart-manifest",
    )
    rans_smoke.add_argument(
        "--restart-manifest",
        type=Path,
        default=None,
        help="explicit PASS first-order SU2 run_manifest.json for order 2",
    )
    rans_smoke.add_argument("--max-iterations", type=int, default=20)
    rans_smoke.add_argument("--timeout", type=float, default=840.0)
    rans_smoke.add_argument("--quiet", action="store_true")
    rans_smoke.add_argument("--output", type=Path, required=True)
    rans_smoke.add_argument("--tools-config", type=Path, default=DEFAULT_TOOLS_CONFIG)
    for tool in ("su2_cfd", "pvbatch"):
        rans_smoke.add_argument(
            _tool_option(tool),
            dest=tool,
            type=Path,
            default=None,
            help=f"override {tool} path (takes priority over {ENV_VARS[tool]})",
        )
    rans_smoke.set_defaults(handler=_handle_pipeline_rans_smoke)

    rear_outlet_freeze_check = pipeline_sub.add_parser(
        "rear-outlet-freeze-check",
        help="evaluate hash-pinned rear-outlet freeze evidence without external tools",
    )
    rear_outlet_freeze_check.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_REAR_OUTLET_FREEZE_CONFIG,
        help="rear-outlet freeze evidence contract",
    )
    rear_outlet_freeze_check.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new or existing run directory under runs/rear_outlet_freeze",
    )
    rear_outlet_freeze_check.set_defaults(
        handler=_handle_pipeline_rear_outlet_freeze_check
    )

    return parser


def _cli_overrides(args: argparse.Namespace) -> dict[str, Path]:
    return {
        name: value
        for name in TOOL_NAMES
        if (value := getattr(args, name, None)) is not None
    }


def _toolchain(args: argparse.Namespace) -> Toolchain:
    return Toolchain(
        config_path=args.tools_config,
        cli_overrides=_cli_overrides(args),
    )


def _runner(args: argparse.Namespace, *, toolchain: Toolchain) -> CommandRunner:
    return CommandRunner(
        output_dir=args.output_dir,
        live_output=not args.quiet,
        allow_mnt_c_executables=toolchain.allow_mnt_c_executables,
    )


def _resolved_payload(resolved: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        name: {
            "path": str(tool.path),
            "source": tool.source,
        }
        for name, tool in resolved.items()
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _artifact_path(output_dir: Path, name: str) -> Path:
    resolved_output_dir = output_dir.expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return resolved_output_dir / f"{name}-{timestamp}-{uuid4().hex[:8]}.json"


def _write_artifact(output_dir: Path, name: str, payload: object) -> Path:
    path = _artifact_path(output_dir, name)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _handle_tools_check(args: argparse.Namespace) -> int:
    resolved = _toolchain(args).check()
    payload = {"status": "ok", "tools": _resolved_payload(resolved)}
    artifact = _write_artifact(args.output_dir, "tools-check", payload)
    _print_json({**payload, "artifact": str(artifact)})
    return 0


def _handle_gmsh_smoke(args: argparse.Namespace) -> int:
    output_directory = args.output.expanduser().resolve()
    connection_root = DEFAULT_OUTPUT_DIR.resolve()
    if output_directory != connection_root and connection_root not in output_directory.parents:
        raise ValueError(
            f"Gmsh connection output must stay under {connection_root}: "
            f"{output_directory}"
        )
    if args.timeout is not None and args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")

    args.output_dir = output_directory
    toolchain = _toolchain(args)
    bridge = GmshBridge(
        runner=_runner(args, toolchain=toolchain),
        toolchain=toolchain,
    )
    manifest = bridge.smoke(
        output_directory=output_directory,
        timeout=args.timeout,
    )
    _print_json(manifest)
    return 0


def _handle_gmsh_catalog(args: argparse.Namespace) -> int:
    if ".." in args.step.expanduser().parts:
        raise ValueError("gmsh catalog --step must not contain '..'")
    step_path = args.step.expanduser().resolve(strict=True)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if step_path != repository_root and repository_root not in step_path.parents:
        raise ValueError(
            f"gmsh catalog STEP must stay within the repository: {step_path}"
        )
    if not step_path.is_file():
        raise ValueError(f"gmsh catalog STEP is not a file: {step_path}")
    if step_path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError("gmsh catalog --step must use a .step or .stp file")

    if ".." in args.output.expanduser().parts:
        raise ValueError("gmsh catalog --output must not contain '..'")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    lexical_required = Path(
        os.path.abspath(DEFAULT_GEOMETRY_INSPECTION_OUTPUT_DIR)
    )
    if os.path.normcase(str(lexical_output)) != os.path.normcase(
        str(lexical_required)
    ):
        raise ValueError(
            "gmsh catalog --output must exactly be the repository geometry "
            f"inspection directory: {lexical_required}"
        )
    output_directory = lexical_output.resolve(strict=False)
    required_output = lexical_required.resolve(strict=False)
    if os.path.normcase(str(output_directory)) != os.path.normcase(
        str(required_output)
    ):
        raise ValueError("gmsh catalog output resolves outside the required directory")

    manifest = GmshBridge().inspect_step_geometry(
        step_path,
        output_directory,
        allowed_output_root=required_output,
    )
    _print_json(manifest)
    return 0


def _handle_gmsh_review(args: argparse.Namespace) -> int:
    for label, raw_path in (
        ("step", args.step),
        ("project", args.project),
        ("output", args.output),
        ("tools-config", args.tools_config),
    ):
        if ".." in raw_path.expanduser().parts:
            raise ValueError(f"gmsh review --{label} must not contain '..'")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    step_path = args.step.expanduser().resolve(strict=True)
    if repository_root not in step_path.parents:
        raise ValueError(f"gmsh review STEP must stay inside {repository_root}")
    if not step_path.is_file() or step_path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError(f"gmsh review requires a STEP file: {step_path}")
    project_path = args.project.expanduser().resolve(strict=True)
    required_project = (repository_root / "config" / "project.toml").resolve(
        strict=True
    )
    if project_path != required_project:
        raise ValueError(
            f"gmsh review project must exactly be {required_project}: {project_path}"
        )
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    lexical_required = Path(os.path.abspath(DEFAULT_GEOMETRY_REVIEW_OUTPUT_DIR))
    if os.path.normcase(str(lexical_output)) != os.path.normcase(
        str(lexical_required)
    ):
        raise ValueError(
            f"gmsh review output must exactly be {lexical_required}: {lexical_output}"
        )
    output = lexical_output.resolve(strict=False)
    if repository_root not in output.parents:
        raise ValueError("gmsh review output resolves outside the repository")
    if not math.isfinite(args.mesh_size) or args.mesh_size <= 0:
        raise ValueError("--mesh-size must be finite and greater than zero")
    if not 1 <= args.max_triangles <= 100_000:
        raise ValueError("--max-triangles must be between 1 and 100000")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")

    args.output_dir = output
    manifest = run_geometry_evidence_review(
        step_path=step_path,
        project_path=project_path,
        output_directory=output,
        allowed_output_root=lexical_required.resolve(strict=False),
        toolchain=_toolchain(args),
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
        mesh_size_m=args.mesh_size,
        max_triangles=args.max_triangles,
        controller_argv=getattr(args, "controller_argv", ()),
    )
    _print_json(manifest)
    return 0


def _handle_gmsh_repair(args: argparse.Namespace) -> int:
    if ".." in args.step.expanduser().parts:
        raise ValueError("gmsh repair --step must not contain '..'")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    step_path = args.step.expanduser().resolve(strict=True)
    required_step = (repository_root / "geometry" / "raw" / "model1.step").resolve(
        strict=True
    )
    if step_path != required_step:
        raise ValueError(
            f"gmsh repair --step must exactly be the canonical input {required_step}"
        )
    review = repository_root / "runs" / "real_connection" / "geometry_review"
    manifest = repair_shared_topology(
        step_path,
        DEFAULT_DERIVED_GEOMETRY_DIR,
        DEFAULT_GEOMETRY_REPAIR_OUTPUT_DIR,
        step_topology_path=review / "step_topology.json",
        interface_evidence_path=review / "interface_completeness.json",
        boundary_candidates_path=review / "boundary_semantic_candidates.json",
        confirmation_document_path=review / "marker_confirmation_confirmed.json",
        confirmation_validation_path=review / "marker_confirmation_validation.json",
        original_surface_catalog_path=review / "surface_catalog.csv",
        original_volume_catalog_path=review / "volume_catalog.csv",
        repository_root=repository_root,
    )
    _print_json(manifest)
    return 0


def _handle_gmsh_validate_repair(args: argparse.Namespace) -> int:
    for label, value in (("--step", args.step), ("--brep", args.brep), ("--output", args.output)):
        if ".." in value.expanduser().parts:
            raise ValueError(f"gmsh validate-repair {label} must not contain '..'")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    step_path = args.step.expanduser().resolve(strict=True)
    brep_path = args.brep.expanduser().resolve(strict=True)
    output_directory = args.output.expanduser().resolve(strict=False)
    required_step = (repository_root / "geometry" / "raw" / "model1.step").resolve(
        strict=True
    )
    required_brep = (
        DEFAULT_DERIVED_GEOMETRY_DIR / "model1_shared_topology.brep"
    ).resolve(strict=True)
    required_output = DEFAULT_GEOMETRY_REPAIR_VALIDATION_OUTPUT_DIR.resolve(
        strict=False
    )
    if step_path != required_step:
        raise ValueError(
            "gmsh validate-repair --step must exactly be the canonical input "
            f"{required_step}"
        )
    if brep_path != required_brep:
        raise ValueError(
            "gmsh validate-repair --brep must exactly be the pipeline BREP "
            f"{required_brep}"
        )
    if output_directory != required_output:
        raise ValueError(
            "gmsh validate-repair --output must exactly be " f"{required_output}"
        )
    review = repository_root / "runs" / "real_connection" / "geometry_review"
    repair = DEFAULT_GEOMETRY_REPAIR_OUTPUT_DIR
    report = validate_interface_persistence(
        step_path,
        brep_path,
        output_directory / "interface_persistence.json",
        project_path=repository_root / "config" / "project.toml",
        step_topology_path=review / "step_topology.json",
        interface_evidence_path=review / "interface_completeness.json",
        boundary_candidates_path=review / "boundary_semantic_candidates.json",
        confirmation_document_path=review / "marker_confirmation_confirmed.json",
        confirmation_validation_path=review / "marker_confirmation_validation.json",
        original_surface_catalog_path=review / "surface_catalog.csv",
        original_volume_catalog_path=review / "volume_catalog.csv",
        repair_manifest_path=repair / "repair_manifest.json",
        shared_interface_path=repair / "shared_interface.json",
    )
    _print_json(report)
    return 0


def _handle_gmsh_markers(args: argparse.Namespace) -> int:
    if ".." in args.output.expanduser().parts:
        raise ValueError("gmsh markers --output must not contain '..'")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    output = Path(os.path.abspath(args.output.expanduser()))
    required_output = Path(os.path.abspath(DEFAULT_MARKERS_CONFIG))
    if os.path.normcase(str(output)) != os.path.normcase(str(required_output)):
        raise ValueError(
            f"gmsh markers --output must exactly be {required_output}: {output}"
        )
    evidence = DEFAULT_MARKER_CONFIG_EVIDENCE_DIR.resolve(strict=False)
    if evidence.exists() or evidence.is_symlink():
        raise ValueError(f"refusing to overwrite marker evidence directory: {evidence}")
    evidence.mkdir(parents=True)
    started = datetime.now(timezone.utc).isoformat()
    manifest_path = evidence / "marker_config_manifest.json"
    log_path = evidence / "marker_config.log"
    review = repository_root / "runs" / "real_connection"
    repair = review / "geometry_repair"
    try:
        document = write_marker_config(
            output,
            project_path=repository_root / "config" / "project.toml",
            source_step_path=repository_root / "geometry" / "raw" / "model1.step",
            pipeline_brep_path=(
                DEFAULT_DERIVED_GEOMETRY_DIR / "model1_shared_topology.brep"
            ),
            repair_manifest_path=repair / "repair_manifest.json",
            marker_rematch_path=repair / "marker_rematch.json",
            interface_persistence_path=(
                DEFAULT_GEOMETRY_REPAIR_VALIDATION_OUTPUT_DIR
                / "interface_persistence.json"
            ),
            pipeline_topology_path=repair / "pipeline_brep_topology.json",
            repository_root=repository_root,
        )
    except BaseException as error:
        ended = datetime.now(timezone.utc).isoformat()
        failure = {
            "status": "FAIL",
            "stage": "marker_config",
            "started_at": started,
            "ended_at": ended,
            "output": str(output),
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(failure, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        log_path.write_text(
            f"status: FAIL\nerror_type: {type(error).__name__}\nerror_message: {error}\n",
            encoding="utf-8",
        )
        raise
    ended = datetime.now(timezone.utc).isoformat()
    solver_markers = document["solver_markers"]
    manifest = {
        "status": "PASS",
        "stage": "marker_config",
        "started_at": started,
        "ended_at": ended,
        "schema": document["schema"],
        "marker_config": {
            "path": str(output),
            "size_bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "payload_sha256": document["integrity"]["payload_sha256"],
        },
        "pipeline_geometry": document["pipeline_geometry"],
        "solver_markers": {
            marker["physical_name"]: marker["member_count"]
            for marker in solver_markers
        },
        "external_surface_member_count": sum(
            marker["member_count"] for marker in solver_markers
        ),
        "fluid_volume_count": document["fluid"]["volume_count"],
        "measurement": {
            "name": document["measurement"]["name"],
            "solver_boundary": document["measurement"]["solver_boundary"],
            "create_physical_group": document["measurement"][
                "create_physical_group"
            ],
        },
        "runtime_entity_tags_in_config": False,
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    log_path.write_text(
        "status: PASS\n"
        f"marker_config: {output}\n"
        f"sha256: {manifest['marker_config']['sha256']}\n"
        "runtime_entity_tags_in_config: false\n",
        encoding="utf-8",
    )
    _print_json(manifest)
    return 0


def _handle_gmsh_step(args: argparse.Namespace) -> int:
    GmshBridge().step(args.step_file)
    return 0


def _handle_su2_smoke(args: argparse.Namespace) -> int:
    output_directory = args.output.expanduser().resolve()
    connection_root = DEFAULT_OUTPUT_DIR.resolve()
    if output_directory != connection_root and connection_root not in output_directory.parents:
        raise ValueError(
            f"SU2 connection output must stay under {connection_root}: "
            f"{output_directory}"
        )
    if args.nproc <= 0:
        raise ValueError("--nproc must be greater than zero")
    if not 5 <= args.max_iterations <= 10:
        raise ValueError("--max-iterations must be between 5 and 10")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")

    args.output_dir = output_directory
    toolchain = _toolchain(args)
    su2_cfd = toolchain.resolve("su2_cfd")
    su2_sol = toolchain.resolve_optional("su2_sol")
    mpiexec = (
        toolchain.resolve("mpiexec")
        if args.nproc > 1
        else toolchain.resolve_optional("mpiexec")
    )
    manifest = SU2Bridge(
        _runner(args, toolchain=toolchain),
        su2_cfd=su2_cfd.path,
        su2_sol=None if su2_sol is None else su2_sol.path,
        mpiexec=None if mpiexec is None else mpiexec.path,
    ).run_smoke_case(
        args.mesh,
        output_directory,
        nproc=args.nproc,
        max_iterations=args.max_iterations,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
    )
    _print_json(manifest)
    return 0


def _handle_su2_run(args: argparse.Namespace) -> int:
    if args.nproc <= 0:
        raise ValueError("--nproc must be greater than zero")
    toolchain = _toolchain(args)
    su2_cfd = toolchain.resolve("su2_cfd")
    mpiexec = toolchain.resolve("mpiexec") if args.nproc > 1 else None
    config_path = args.config_file.expanduser().resolve()
    result = SU2Bridge(
        _runner(args, toolchain=toolchain),
        su2_cfd=su2_cfd.path,
        mpiexec=None if mpiexec is None else mpiexec.path,
    ).run(
        config_path,
        nproc=args.nproc,
        cwd=config_path.parent,
        timeout=args.timeout,
        dry_run=args.dry_run,
        live_output=not args.quiet,
    )
    _print_json(result.as_metadata())
    return 0


def _handle_paraview_inspect(args: argparse.Namespace) -> int:
    output_directory = args.output.expanduser().resolve()
    connection_root = DEFAULT_OUTPUT_DIR.resolve()
    if (
        output_directory != connection_root
        and connection_root not in output_directory.parents
    ):
        raise ValueError(
            f"ParaView connection output must stay under {connection_root}: "
            f"{output_directory}"
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")

    args.output_dir = output_directory
    toolchain = _toolchain(args)
    pvbatch = toolchain.resolve("pvbatch")
    manifest = ParaViewBridge(
        _runner(args, toolchain=toolchain),
        pvbatch=pvbatch.path,
        script_dir=DEFAULT_PARAVIEW_SCRIPTS,
        pvbatch_source=pvbatch.source,
    ).inspect_manifest(
        args.manifest,
        output_directory,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
    )
    _print_json(manifest)
    return 0


def _connection_stages(args: argparse.Namespace) -> tuple[PipelineStage, ...]:
    toolchain = _toolchain(args)
    runner = _runner(args, toolchain=toolchain)
    output_dir = Path(args.output_dir).expanduser().resolve()

    def gmsh_smoke() -> None:
        if args.dry_run:
            payload = {
                "status": "dry-run",
                "module": "gmsh",
                "action": "import-initialize-finalize",
            }
        else:
            payload = {"status": "ok", **GmshBridge().smoke()}
        _write_artifact(output_dir, "gmsh-smoke", payload)

    def su2_smoke() -> None:
        su2_cfd = toolchain.resolve("su2_cfd")
        SU2Bridge(runner, su2_cfd=su2_cfd.path).smoke(
            timeout=args.timeout,
            dry_run=args.dry_run,
            live_output=not args.quiet,
        )

    def paraview_smoke() -> None:
        pvbatch = toolchain.resolve("pvbatch")
        smoke_output = _artifact_path(output_dir, "paraview-smoke")
        bridge = ParaViewBridge(
            runner, pvbatch=pvbatch.path, script_dir=DEFAULT_PARAVIEW_SCRIPTS
        )
        bridge.run_script(
            "probe_pvbatch.py",
            args=("--output", str(smoke_output)),
            timeout=args.timeout,
            dry_run=args.dry_run,
            live_output=not args.quiet,
            name="paraview-smoke",
        )

    return (
        PipelineStage("gmsh-import", gmsh_smoke),
        PipelineStage("su2-cfd", su2_smoke),
        PipelineStage("paraview-pvbatch", paraview_smoke),
    )


def _run_connection_pipeline(args: argparse.Namespace, *, name: str) -> int:
    result = Pipeline(args.output_dir).run(_connection_stages(args), name=name)
    _print_json(
        {
            "status": "ok",
            "pipeline": result.name,
            "log": str(result.log_path),
            "stages": [record.name for record in result.records],
        }
    )
    return 0


def _handle_connect_test(args: argparse.Namespace) -> int:
    if ".." in args.output.expanduser().parts:
        raise ValueError("connect-test --output must not contain '..'")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    lexical_required = Path(os.path.abspath(DEFAULT_OUTPUT_DIR))
    if os.path.normcase(str(lexical_output)) != os.path.normcase(
        str(lexical_required)
    ):
        raise ValueError(
            "connect-test --output must exactly be the repository connection root: "
            f"{lexical_required}"
        )
    resolved_output = lexical_output.resolve(strict=False)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if resolved_output != repository_root and repository_root not in resolved_output.parents:
        raise ValueError("connect-test output resolves outside the repository")
    if args.nproc != 1:
        raise ValueError("connect-test is serial; --nproc must be 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")

    args.output_dir = lexical_output
    result = run_connect_test(
        output_directory=lexical_output,
        allowed_output_root=lexical_required,
        trusted_repository_root=REPOSITORY_ROOT,
        toolchain=_toolchain(args),
        paraview_script_directory=DEFAULT_PARAVIEW_SCRIPTS,
        clean=args.clean,
        nproc=args.nproc,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
        controller_argv=getattr(args, "controller_argv", ()),
    )
    _print_json(result.report)
    return 0


def _handle_pipeline_run(args: argparse.Namespace) -> int:
    if ".." in args.output.expanduser().parts:
        raise ValueError("pipeline run --output must not contain '..'")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    lexical_required = Path(os.path.abspath(DEFAULT_REAL_CONNECTION_OUTPUT_DIR))
    if os.path.normcase(str(lexical_output)) != os.path.normcase(
        str(lexical_required)
    ):
        raise ValueError(
            "pipeline run --output must exactly be the repository real connection "
            f"root: {lexical_required}"
        )
    resolved_output = lexical_output.resolve(strict=False)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if resolved_output != repository_root and repository_root not in resolved_output.parents:
        raise ValueError("pipeline run output resolves outside the repository")
    if args.mesh_level != "smoke":
        raise ValueError("pipeline run only permits --mesh-level smoke")
    if args.nproc != 1:
        raise ValueError("pipeline run is serial; --nproc must be 1")
    if not 1 <= args.max_iterations <= 5:
        raise ValueError("--max-iterations must be between 1 and 5")
    if (
        not math.isfinite(args.smoke_characteristic_length)
        or not 0.01 <= args.smoke_characteristic_length <= 2.0
    ):
        raise ValueError(
            "--smoke-characteristic-length must be between 0.01 and 2.0 m"
        )
    if (
        not math.isfinite(args.facet_overlap_angle_tolerance)
        or not 1.0e-6 <= args.facet_overlap_angle_tolerance <= 0.1
    ):
        raise ValueError(
            "--facet-overlap-angle-tolerance must be between 1e-6 and 0.1 degree"
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")

    result = run_real_connection(
        step_path=Path(os.path.abspath(args.step.expanduser())),
        project_path=Path(os.path.abspath(args.project.expanduser())),
        cases_path=Path(os.path.abspath(args.cases.expanduser())),
        markers_path=Path(os.path.abspath(args.markers.expanduser())),
        topology_smoke_path=Path(
            os.path.abspath(args.topology_smoke_config.expanduser())
        ),
        case_id=args.case_id,
        mesh_level=args.mesh_level,
        nproc=args.nproc,
        max_iterations=args.max_iterations,
        output_directory=lexical_output,
        allowed_output_root=lexical_required,
        trusted_repository_root=REPOSITORY_ROOT,
        toolchain=_toolchain(args),
        paraview_script_directory=DEFAULT_PARAVIEW_SCRIPTS,
        smoke_characteristic_length_m=args.smoke_characteristic_length,
        facet_overlap_angle_tolerance_degrees=(
            args.facet_overlap_angle_tolerance
        ),
        gmsh_algorithm_3d=args.gmsh_algorithm_3d,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
        controller_argv=getattr(args, "controller_argv", ()),
    )
    _print_json(result.report)
    return 0


def _handle_pipeline_outlet_check(args: argparse.Namespace) -> int:
    if not 20 <= args.max_iterations <= 500:
        raise ValueError("--max-iterations must be between 20 and 500")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")
    report = run_outlet_validation(
        step_path=Path(os.path.abspath(args.step.expanduser())),
        project_path=Path(os.path.abspath(args.project.expanduser())),
        cases_path=Path(os.path.abspath(args.cases.expanduser())),
        markers_path=Path(os.path.abspath(args.markers.expanduser())),
        topology_smoke_path=Path(
            os.path.abspath(args.topology_smoke_config.expanduser())
        ),
        case_id=args.case_id,
        source_mesh_path=Path(os.path.abspath(args.mesh.expanduser())),
        mesh_manifest_path=Path(os.path.abspath(args.mesh_manifest.expanduser())),
        output_directory=Path(os.path.abspath(args.output.expanduser())),
        trusted_repository_root=REPOSITORY_ROOT,
        toolchain=_toolchain(args),
        paraview_script_directory=DEFAULT_PARAVIEW_SCRIPTS,
        max_iterations=args.max_iterations,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
        controller_argv=getattr(args, "controller_argv", ()),
    )
    _print_json(report)
    return 0


def _handle_pipeline_boundary_layer_trial(args: argparse.Namespace) -> int:
    if ".." in args.output.expanduser().parts:
        raise ValueError("boundary-layer trial --output must not contain '..'")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    lexical_root = Path(os.path.abspath(DEFAULT_BOUNDARY_LAYER_TRIAL_OUTPUT_ROOT))
    resolved_output = lexical_output.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=False)
    if resolved_output == resolved_root or resolved_root not in resolved_output.parents:
        raise ValueError(
            "boundary-layer trial output must be a new child of "
            f"{lexical_root}"
        )
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if repository_root not in resolved_output.parents:
        raise ValueError("boundary-layer trial output resolves outside the repository")

    inputs = load_real_connection_inputs(
        step_path=Path(os.path.abspath(args.step.expanduser())),
        project_path=Path(os.path.abspath(args.project.expanduser())),
        cases_path=Path(os.path.abspath(args.cases.expanduser())),
        markers_path=Path(os.path.abspath(args.markers.expanduser())),
        topology_smoke_path=Path(
            os.path.abspath(args.topology_smoke_config.expanduser())
        ),
        case_id=args.case_id,
        trusted_repository_root=REPOSITORY_ROOT,
    )
    selected_case = select_real_case(inputs)
    manifest = build_boundary_layer_trial(
        inputs.pipeline_geometry_path,
        lexical_output,
        project=inputs.project,
        selected_case=selected_case,
        marker_config=inputs.marker_document,
        trial_config_path=Path(os.path.abspath(args.trial_config.expanduser())),
        input_records=inputs.input_records,
    )
    _print_json(manifest)
    return 0


def _read_cfdpipe_config(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if ".." in path.expanduser().parts:
        raise ValueError(f"{label} path must not contain '..'")
    lexical = Path(os.path.abspath(path.expanduser()))
    config_root = (REPOSITORY_ROOT / "config").resolve(strict=True)
    resolved = lexical.resolve(strict=True)
    if resolved.parent != config_root or not resolved.is_file():
        raise ValueError(f"{label} must be a regular file directly under {config_root}")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            raw = stream.read()
        digest.update(raw)
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    return document, digest.hexdigest()


def _handle_pipeline_boundary_layer_smoke(args: argparse.Namespace) -> int:
    if ".." in args.output.expanduser().parts:
        raise ValueError("boundary-layer smoke --output must not contain '..'")
    lexical_output = Path(os.path.abspath(args.output.expanduser()))
    resolved_output = lexical_output.resolve(strict=False)
    lexical_roots = [
        Path(os.path.abspath(DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT)),
        Path(os.path.abspath(DEFAULT_BOUNDARY_LAYER_QUALITY_OUTPUT_ROOT)),
    ]
    resolved_roots = [value.resolve(strict=False) for value in lexical_roots]
    if not any(
        resolved_output != root and root in resolved_output.parents
        for root in resolved_roots
    ):
        raise ValueError(
            "boundary-layer smoke output must be a new child of one of: "
            + ", ".join(str(value) for value in lexical_roots)
        )
    if lexical_output.exists():
        raise ValueError("boundary-layer smoke output must not already exist")
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if repository_root not in resolved_output.parents:
        raise ValueError("boundary-layer smoke output resolves outside the repository")

    inputs = load_real_connection_inputs(
        step_path=Path(os.path.abspath(args.step.expanduser())),
        project_path=Path(os.path.abspath(args.project.expanduser())),
        cases_path=Path(os.path.abspath(args.cases.expanduser())),
        markers_path=Path(os.path.abspath(args.markers.expanduser())),
        topology_smoke_path=Path(
            os.path.abspath(args.topology_smoke_config.expanduser())
        ),
        case_id=args.case_id,
        trusted_repository_root=REPOSITORY_ROOT,
    )
    selected_case = select_real_case(inputs)
    smoke_document, smoke_config_hash = _read_cfdpipe_config(
        args.smoke_config, "smoke config"
    )
    schedule_document, schedule_hash = _read_cfdpipe_config(
        args.schedule_config, "schedule config"
    )
    topology_document, topology_hash = _read_cfdpipe_config(
        args.topology_smoke_config, "topology-smoke config"
    )
    schedule_contract = smoke_document.pop("schedule", None)
    expected_schedule_contract = {
        "source",
        "trial_config_sha256",
        "first_layer_height_field",
        "direction_probe_distance_field",
    }
    if not isinstance(schedule_contract, dict) or set(schedule_contract) != expected_schedule_contract:
        raise ValueError("boundary-layer smoke schedule contract is incomplete")
    if (
        schedule_contract["source"]
        != "cfdpipe.boundary_layer_trial.calculate_boundary_layer_schedule"
        or str(schedule_contract["trial_config_sha256"]).casefold() != schedule_hash
        or schedule_contract["first_layer_height_field"] != "mesh.first_layer_height_m"
        or schedule_contract["direction_probe_distance_field"]
        != "mesh.direction_probe_distance_m"
    ):
        raise ValueError("boundary-layer smoke schedule contract is stale")

    input_records = dict(inputs.input_records)
    topology_record = input_records.get("topology_smoke_config")
    if not isinstance(topology_record, dict) or topology_record.get("sha256") != topology_hash:
        raise ValueError("topology-smoke config changed after the real input gate")
    input_records["topology_smoke"] = dict(topology_record)
    trial_normalized = normalize_boundary_layer_trial_config(
        schedule_document,
        project=inputs.project,
        selected_case=selected_case,
        marker_config=inputs.marker_document,
        input_records=input_records,
    )
    schedule = calculate_boundary_layer_schedule(trial_normalized, selected_case)
    mesh = smoke_document.get("mesh")
    if not isinstance(mesh, dict):
        raise ValueError("boundary-layer smoke mesh table is missing")
    expected_first = float(schedule["first_layer_height_m"])
    expected_probe = float(schedule["total_thickness_m"])
    if not math.isclose(
        float(mesh.get("first_layer_height_m", math.nan)),
        expected_first,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    ) or not math.isclose(
        float(mesh.get("direction_probe_distance_m", math.nan)),
        expected_probe,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    ):
        raise ValueError("boundary-layer smoke first-layer schedule is stale")
    mesh["first_layer_height_m"] = expected_first
    mesh["direction_probe_distance_m"] = expected_probe
    normalized = normalize_boundary_layer_smoke_config(
        smoke_document,
        project=inputs.project,
        marker_config=inputs.marker_document,
        input_records=input_records,
    )
    strategy = RealProjectBoundaryLayerStrategy(
        inputs.marker_document,
        topology_document,
        marker_config_sha256=str(input_records["markers"]["sha256"]),
        topology_smoke_config_sha256=topology_hash,
    )
    run_evidence = {
        "controller_argv": list(getattr(args, "controller_argv", ())),
        "input_records": input_records,
        "configuration_files": {
            "smoke": {
                "path": str(Path(os.path.abspath(args.smoke_config.expanduser()))),
                "sha256": smoke_config_hash,
            },
            "schedule": {
                "path": str(Path(os.path.abspath(args.schedule_config.expanduser()))),
                "sha256": schedule_hash,
            },
            "topology_smoke": {
                "path": str(Path(os.path.abspath(args.topology_smoke_config.expanduser()))),
                "sha256": topology_hash,
            },
        },
        "boundary_layer_schedule": schedule,
        "output_directory": str(lexical_output),
        "downstream_programs_called": False,
    }
    manifest = build_boundary_layer_smoke(
        inputs.pipeline_geometry_path,
        lexical_output,
        normalized,
        strategy=strategy,
        run_evidence=run_evidence,
    )
    _print_json(manifest)
    return 0


def _handle_pipeline_boundary_layer_clearance(args: argparse.Namespace) -> int:
    manifest = run_boundary_layer_clearance_command(
        config_path=args.config,
        markers_path=args.markers,
        output_directory=args.output,
        repository_root=REPOSITORY_ROOT,
    )
    # The evidence contains thousands of individual isInside probes and is
    # intentionally persisted to disk.  Keep terminal output bounded so a
    # successful read-only audit cannot flood the controller's stdout pipe.
    _print_json(
        {
            "status": manifest.get("status"),
            "execution_status": manifest.get("execution_status"),
            "clearance_requirement_status": manifest.get(
                "clearance_requirement_status"
            ),
            "surface_count": manifest.get("surface_count"),
            "sample_count": manifest.get("sample_count"),
            "minimum_clearance_lower_bound_m": manifest.get(
                "minimum_clearance_lower_bound_m"
            ),
            "report_path": manifest.get("report_path"),
        }
    )
    return 0 if manifest.get("status") == "PASS" else 1


def _handle_pipeline_boundary_layer_local_plan(args: argparse.Namespace) -> int:
    manifest = run_boundary_layer_local_schedule_command(
        config_path=args.config,
        clearance_report_path=args.clearance_report,
        clearance_report_sha256=args.clearance_sha256,
        output_directory=args.output,
        repository_root=REPOSITORY_ROOT,
    )
    # Per-surface schedules remain in the strict JSON artifact; print only the
    # bounded hand-off summary needed by an orchestrator.
    _print_json(
        {
            "status": manifest.get("status"),
            "surface_count": manifest.get("surface_count"),
            "pass_surface_count": manifest.get("pass_surface_count"),
            "fail_surface_count": manifest.get("fail_surface_count"),
            "mesh_generated": manifest.get("mesh_generated"),
            "production_mesh_eligible": manifest.get(
                "production_mesh_eligible"
            ),
            "report_path": manifest.get("report_path"),
        }
    )
    return 0 if manifest.get("status") == "PASS" else 1


def _coarse_calibration_failure_report(
    *,
    stage: str,
    code: str,
    message: str,
    config_path: Path,
    output_directory: Path,
    characteristic_length_m: float,
    controller_argv: list[str],
) -> dict[str, Any]:
    return {
        "schema": COARSE_CALIBRATION_PREFLIGHT_SCHEMA,
        "status": "FAIL",
        "stage": stage,
        "policy": {"command_runner_constructed": False},
        "inputs": {
            "config_path": str(config_path),
            "output_directory": str(output_directory),
            "selected_characteristic_length_m": characteristic_length_m,
            "controller_argv": list(controller_argv),
        },
        "projections": {},
        "requirements": {},
        "checks": {"cli_preflight": False},
        "reasons": [{"code": code, "message": message}],
    }


def _write_coarse_calibration_preflight(
    evidence_directory: Path, report: dict[str, Any]
) -> Path:
    path = evidence_directory / "calibration_preflight.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return path


def _write_coarse_projection_preflight(
    evidence_directory: Path, report: dict[str, Any]
) -> Path:
    path = evidence_directory / "projection_preflight.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return path


def _make_physical_memory_abort_check(
    contract: dict[str, Any],
):
    """Return a fail-closed monitor protecting the configured OS RAM reserve.

    Preflight reserves the worker hard limit in addition to the OS reserve and
    monitor margin.  Once the worker is running, its allocation is reflected in
    system available memory, so the controller aborts before availability drops
    below ``OS reserve + monitor margin``.  Any Win32 sampling failure is
    deliberately allowed to propagate: ``CommandRunner`` then terminates the
    child and preserves the original traceback and command logs.
    """

    resource = contract.get("resource")
    if not isinstance(resource, dict):
        raise ValueError("coarse contract resource section is unavailable")
    reserve = resource.get("os_physical_memory_reserve_bytes")
    margin = resource.get("worker_monitor_margin_bytes")
    for name, value in (
        ("os_physical_memory_reserve_bytes", reserve),
        ("worker_monitor_margin_bytes", margin),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"coarse contract {name} must be a positive integer")
    protected_available = int(reserve) + int(margin)

    def check() -> str | None:
        report = capture_system_physical_memory()
        if report.get("status") != "PASS":
            raise RuntimeError("physical-memory monitor did not return PASS")
        metrics = report.get("metrics")
        if not isinstance(metrics, dict):
            raise RuntimeError("physical-memory monitor metrics are unavailable")
        available = metrics.get("physical_available_bytes")
        if (
            not isinstance(available, int)
            or isinstance(available, bool)
            or available <= 0
        ):
            raise RuntimeError(
                "physical-memory monitor returned an invalid available byte count"
            )
        if available < protected_available:
            return (
                f"physical available memory {available} bytes is below protected "
                f"OS reserve plus monitor margin {protected_available} bytes"
            )
        return None

    return check


def _handle_pipeline_coarse_mesh_project(args: argparse.Namespace) -> int:
    """Run one count-only Gmsh projection behind physical-memory gates."""

    characteristic = float(args.characteristic_length)
    if not math.isfinite(characteristic) or characteristic <= 0.0:
        raise ValueError("--characteristic-length must be finite and positive")
    timeout = float(args.timeout)
    if not math.isfinite(timeout) or not 0.0 < timeout <= 840.0:
        raise ValueError("coarse projection --timeout must be in (0, 840]")

    raw_config = args.config.expanduser()
    if ".." in raw_config.parts:
        raise ValueError("coarse projection --config must not contain '..'")
    config_path = Path(os.path.abspath(raw_config)).resolve(strict=True)
    expected_config = Path(os.path.abspath(DEFAULT_COARSE_MESH_CONFIG)).resolve(
        strict=True
    )
    if (
        os.path.normcase(str(config_path))
        != os.path.normcase(str(expected_config))
        or not config_path.is_file()
        or raw_config.is_symlink()
    ):
        raise ValueError(
            "coarse projection requires the repository config/coarse_mesh.toml"
        )

    raw_output = args.output.expanduser()
    if ".." in raw_output.parts:
        raise ValueError("coarse projection --output must not contain '..'")
    lexical_output = Path(os.path.abspath(raw_output))
    lexical_root = Path(os.path.abspath(DEFAULT_COARSE_MESH_OUTPUT_ROOT))
    resolved_output = lexical_output.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=True)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if (
        resolved_output == resolved_root
        or resolved_root not in resolved_output.parents
        or repository_root not in resolved_output.parents
        or resolved_output.parent != resolved_root
        or lexical_output.exists()
        or resolved_output.exists()
        or lexical_output.is_symlink()
    ):
        raise ValueError(
            "coarse projection output must be a new direct child of runs/mesh/coarse"
        )
    if resolved_output.name.casefold() == "_worker_evidence":
        raise ValueError("coarse projection output uses the reserved evidence name")

    evidence_directory = resolved_root / "_worker_evidence" / resolved_output.name
    if evidence_directory.exists() or evidence_directory.is_symlink():
        raise ValueError("coarse projection evidence directory must be new")
    evidence_directory.mkdir(parents=True, exist_ok=False)

    try:
        config_document, config_sha256 = _read_cfdpipe_config(
            config_path, "coarse mesh config"
        )
        contract = normalize_coarse_mesh_config(
            config_document, repository_root=repository_root
        )
    except (CoarseMeshError, OSError, ValueError) as error:
        failure = {
            "schema": "cfdpipe.coarse_projection_preflight.v1",
            "status": "FAIL",
            "stage": "CONTRACT",
            "safe_to_launch_projection_worker": False,
            "safe_to_import_gmsh_in_worker": False,
            "inputs": {
                "config_path": str(config_path),
                "output_directory": str(lexical_output),
                "characteristic_length_m": characteristic,
                "controller_argv": list(args.controller_argv),
            },
            "requirements": {},
            "checks": {"contract": False},
            "reasons": [
                {"code": "CONTRACT_INVALID", "message": str(error)}
            ],
        }
        report_path = _write_coarse_projection_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse projection contract preflight failed; report: {report_path}"
        ) from error

    configured = [
        float(value)
        for value in contract.get("calibration_characteristic_lengths_m", [])
    ]
    if not any(
        math.isclose(characteristic, value, rel_tol=1.0e-12, abs_tol=0.0)
        for value in configured
    ):
        failure = {
            "schema": "cfdpipe.coarse_projection_preflight.v1",
            "status": "FAIL",
            "stage": "INPUT",
            "safe_to_launch_projection_worker": False,
            "safe_to_import_gmsh_in_worker": False,
            "inputs": {
                "config_path": str(config_path),
                "config_sha256": config_sha256,
                "output_directory": str(lexical_output),
                "characteristic_length_m": characteristic,
                "configured_characteristic_lengths_m": configured,
                "controller_argv": list(args.controller_argv),
            },
            "requirements": {},
            "checks": {"characteristic_length_authorized": False},
            "reasons": [
                {
                    "code": "CHARACTERISTIC_LENGTH_NOT_AUTHORIZED",
                    "message": "projection characteristic length is not in the contract",
                }
            ],
        }
        report_path = _write_coarse_projection_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse projection input preflight failed; report: {report_path}"
        )

    try:
        captured = capture_system_snapshot(repository_root)
        snapshot = {
            key: int(captured[key])
            for key in (
                "physical_total_bytes",
                "physical_available_bytes",
                "virtual_available_bytes",
                "disk_free_bytes",
            )
        }
        preflight = evaluate_projection_preflight(contract, snapshot)
        preflight["controller"] = {
            "argv": list(args.controller_argv),
            "config_path": str(config_path),
            "config_sha256": config_sha256,
            "coarse_contract_sha256": contract["normalized_config_sha256"],
            "output_directory": str(lexical_output),
            "characteristic_length_m": characteristic,
            "timeout_seconds": timeout,
            "snapshot_disk_path": str(captured["disk_path"]),
            "command_runner_constructed": False,
        }
    except (CoarseCalibrationGateError, KeyError, TypeError, ValueError) as error:
        failure = {
            "schema": "cfdpipe.coarse_projection_preflight.v1",
            "status": "FAIL",
            "stage": "RESOURCE_SNAPSHOT",
            "safe_to_launch_projection_worker": False,
            "safe_to_import_gmsh_in_worker": False,
            "inputs": {},
            "requirements": {},
            "checks": {"resource_snapshot": False},
            "reasons": [
                {"code": "RESOURCE_SNAPSHOT_FAILED", "message": str(error)}
            ],
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            },
        }
        report_path = _write_coarse_projection_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse projection resource snapshot failed; report: {report_path}"
        ) from error

    if preflight.get("status") != "PASS":
        preflight_path = _write_coarse_projection_preflight(
            evidence_directory, preflight
        )
        raise ValueError(
            f"coarse projection resource preflight failed; report: {preflight_path}"
        )

    python_executable = Path(sys.executable).expanduser().resolve(strict=True)
    source_root = (repository_root / "src").resolve(strict=True)
    runner = CommandRunner(
        output_dir=evidence_directory,
        live_output=not args.quiet,
    )
    preflight["controller"]["command_runner_constructed"] = True
    preflight_path = _write_coarse_projection_preflight(
        evidence_directory, preflight
    )
    worker_args = [
        "-m",
        "cfdpipe.coarse_projection_worker",
        "--config",
        str(config_path),
        "--config-sha256",
        config_sha256,
        "--coarse-contract-sha256",
        str(contract["normalized_config_sha256"]),
        "--characteristic-length",
        format(characteristic, ".17g"),
        "--output",
        str(lexical_output),
    ]
    abort_check = _make_physical_memory_abort_check(contract)
    try:
        result = runner.run(
            python_executable,
            worker_args,
            cwd=repository_root,
            env={"PYTHONPATH": str(source_root)},
            timeout=timeout,
            check=True,
            live_output=not args.quiet,
            name="coarse-mesh-projection-worker",
            stdout_log="worker.stdout.log",
            stderr_log="worker.stderr.log",
            metadata_log="worker.command.json",
            abort_check=abort_check,
            abort_check_interval_seconds=0.5,
        )
    except CommandExecutionError as error:
        raise RuntimeError(
            "coarse projection worker failed; evidence remains in "
            f"{evidence_directory}; original stderr:\n{error.stderr}"
        ) from error

    manifest_path = lexical_output / "projection_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("coarse projection worker returned zero without a manifest")
    try:
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        projection_evidence = load_and_validate_projection_evidence(
            manifest_path,
            manifest_sha256,
            output_root=resolved_root,
            expected_config_path=config_path,
            expected_config_sha256=config_sha256,
            expected_coarse_contract_sha256=str(
                contract["normalized_config_sha256"]
            ),
            expected_characteristic_length_m=characteristic,
            expected_process_memory_limit_bytes=int(
                contract["resource"]["projection_worker_memory_limit_bytes"]
            ),
        )
    except (OSError, CoarseProjectionEvidenceError) as error:
        raise ValueError(f"coarse projection manifest failed validation: {error}") from error

    _print_json(
        {
            "status": "PASS",
            "projected_3d_elements": projection_evidence[
                "projected_3d_elements"
            ],
            "manifest": str(manifest_path.resolve()),
            "preflight": str(preflight_path.resolve()),
            "command": result.as_metadata(),
        }
    )
    return 0


def _write_coarse_repair_audit_preflight(
    evidence_directory: Path, report: Mapping[str, Any]
) -> Path:
    path = evidence_directory / "repair_audit_preflight.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            dict(report),
            stream,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        stream.write("\n")
    return path


def _coarse_repair_audit_failure(
    *,
    stage: str,
    code: str,
    message: str,
    config_path: Path,
    output_directory: Path,
    characteristic_length_m: float,
    controller_argv: list[str],
) -> dict[str, Any]:
    return {
        "schema": "cfdpipe.coarse_repair_audit_preflight.v1",
        "status": "FAIL",
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "stage": stage,
        "policy": {"command_runner_constructed": False},
        "inputs": {
            "config_path": str(config_path),
            "output_directory": str(output_directory),
            "selected_characteristic_length_m": characteristic_length_m,
            "controller_argv": list(controller_argv),
        },
        "requirements": {},
        "checks": {"cli_preflight": False},
        "reasons": [{"code": code, "message": message}],
    }


def _canonical_mapping_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _coarse_repair_strict_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"coarse repair audit JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _parse_canonical_utc_z(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} is not canonical UTC Z time")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not canonical UTC Z time") from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed) or value != canonical:
        raise ValueError(f"{label} is not canonical UTC Z time")
    return parsed


def _coarse_repair_path_has_link_component(path: Path) -> bool:
    current = Path(os.path.abspath(path))
    while True:
        try:
            is_junction = getattr(current, "is_junction", None)
            if current.is_symlink() or (
                callable(is_junction) and bool(is_junction())
            ):
                return True
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _coarse_repair_memory_base(
    record: object,
    *,
    schema: str,
    operation: str,
    extra_keys: set[str],
) -> tuple[Mapping[str, Any], int, datetime]:
    if not isinstance(record, Mapping) or set(record) != {
        "schema",
        "status",
        "supported",
        "platform",
        "operation",
        "process_id",
        "captured_at_utc",
        "error",
        *extra_keys,
    }:
        raise ValueError(f"repair audit {operation} evidence schema is incomplete")
    process_id = record.get("process_id")
    if (
        record.get("schema") != schema
        or record.get("status") != "PASS"
        or record.get("supported") is not True
        or record.get("platform") != "win32"
        or record.get("operation") != operation
        or record.get("error") is not None
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise ValueError(f"repair audit {operation} evidence is not a strict PASS")
    captured = _parse_canonical_utc_z(
        record.get("captured_at_utc"), f"repair audit {operation} capture time"
    )
    return record, process_id, captured


def _validate_coarse_repair_system_memory(
    record: object,
) -> tuple[int, datetime]:
    value, process_id, captured = _coarse_repair_memory_base(
        record,
        schema=SYSTEM_REPORT_SCHEMA,
        operation="capture_system_physical_memory",
        extra_keys={"metrics"},
    )
    metrics = value.get("metrics")
    byte_fields = {
        "physical_total_bytes",
        "physical_available_bytes",
        "pagefile_total_bytes",
        "pagefile_available_bytes",
        "virtual_total_bytes",
        "virtual_available_bytes",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != {
        *byte_fields,
        "memory_load_percent",
    }:
        raise ValueError("repair audit system-memory metrics are incomplete")
    if any(
        isinstance(metrics.get(name), bool)
        or not isinstance(metrics.get(name), int)
        or metrics[name] <= 0
        for name in byte_fields
    ):
        raise ValueError("repair audit system-memory byte metrics are invalid")
    load = metrics.get("memory_load_percent")
    if (
        isinstance(load, bool)
        or not isinstance(load, int)
        or not 0 <= load <= 100
        or metrics["physical_available_bytes"] > metrics["physical_total_bytes"]
        or metrics["pagefile_available_bytes"] > metrics["pagefile_total_bytes"]
        or metrics["virtual_available_bytes"] > metrics["virtual_total_bytes"]
    ):
        raise ValueError("repair audit system-memory metrics are inconsistent")
    return process_id, captured


def _validate_coarse_repair_process_memory(
    record: object,
    *,
    limit_bytes: int,
) -> tuple[int, datetime]:
    value, process_id, captured = _coarse_repair_memory_base(
        record,
        schema=PROCESS_REPORT_SCHEMA,
        operation="capture_current_process_memory",
        extra_keys={"metrics", "private_commit_semantics"},
    )
    metrics = value.get("metrics")
    metric_fields = {
        "working_set_bytes",
        "peak_working_set_bytes",
        "private_usage_bytes",
        "pagefile_usage_bytes",
        "peak_pagefile_usage_bytes",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != metric_fields or any(
        isinstance(metrics.get(name), bool)
        or not isinstance(metrics.get(name), int)
        or metrics[name] <= 0
        for name in metric_fields
    ):
        raise ValueError("repair audit process-memory metrics are incomplete")
    if (
        metrics["peak_working_set_bytes"] < metrics["working_set_bytes"]
        or metrics["private_usage_bytes"] != metrics["pagefile_usage_bytes"]
        or metrics["peak_pagefile_usage_bytes"]
        < metrics["private_usage_bytes"]
        or metrics["peak_pagefile_usage_bytes"] > limit_bytes
    ):
        raise ValueError("repair audit private-commit metrics are inconsistent")
    expected_semantics = {
        "schema": PRIVATE_COMMIT_SEMANTICS_SCHEMA,
        "job_object_process_memory_limit_basis": "private_commit_bytes",
        "current_private_commit_metric": "metrics.private_usage_bytes",
        "current_private_commit_source": PRIVATE_COMMIT_CURRENT_SOURCE,
        "peak_private_commit_metric": "metrics.peak_pagefile_usage_bytes",
        "peak_private_commit_source": PRIVATE_COMMIT_PEAK_SOURCE,
        "pagefile_field_means_commit_charge_not_pagefile_residency": True,
        "working_set_is_not_job_process_memory_limit_metric": True,
    }
    if value.get("private_commit_semantics") != expected_semantics:
        raise ValueError("repair audit private-commit semantics are incomplete")
    return process_id, captured


def _validate_coarse_repair_hard_limit(
    record: object,
    *,
    limit_bytes: int,
) -> tuple[int, datetime]:
    value, process_id, captured = _coarse_repair_memory_base(
        record,
        schema=LIMIT_REPORT_SCHEMA,
        operation="install_current_process_memory_limit",
        extra_keys={
            "requested_process_memory_limit_bytes",
            "backend",
            "job_object",
        },
    )
    job = value.get("job_object")
    exact_flags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if (
        value.get("requested_process_memory_limit_bytes") != limit_bytes
        or value.get("backend") not in {"windows_job_object", "linux_rlimit_as"}
        or not isinstance(job, Mapping)
        or set(job)
        != {
            "job_handle_value",
            "limit_flags",
            "process_memory_limit_bytes",
            "process_assigned",
            "handle_retained_for_process_lifetime",
        }
        or isinstance(job.get("job_handle_value"), bool)
        or not isinstance(job.get("job_handle_value"), int)
        or job["job_handle_value"] <= 0
        or job.get("limit_flags") != exact_flags
        or job.get("process_memory_limit_bytes") != limit_bytes
        or job.get("process_assigned") is not True
        or job.get("handle_retained_for_process_lifetime") is not True
    ):
        raise ValueError("repair audit Windows Job Object evidence is incomplete")
    return process_id, captured


def _validate_coarse_repair_audit_pass_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    expected_output_directory: Path,
    expected_config_path: Path,
    expected_config_sha256: str,
    contract: Mapping[str, Any],
    expected_characteristic_length_m: float,
    projection_evidence: Mapping[str, Any],
    local_schedule_binding: Mapping[str, Any],
    expected_schedule_feasibility_endpoint: str | None = None,
    expected_direction_replay_approval: Mapping[str, Any] | None = None,
    expected_direction_replay_inputs: Mapping[str, Any] | None = None,
    expected_physical_schedule_homotopy_endpoint: Mapping[str, Any] | None = None,
    expected_schedule_direction_continuation_endpoint: Mapping[str, Any] | None = None,
    expected_homotopy_audit_evidence: Mapping[str, Any] | None = None,
    allow_physical_incomplete: bool = False,
) -> dict[str, Any]:
    """Reject an incomplete/non-authorizing repair audit after worker exit."""

    if expected_schedule_feasibility_endpoint not in (
        None,
        "minimum-growth-existing-direction-repair",
        "minimum-growth-owner-free-component-direction-audit",
        "minimum-growth-owner-free-component-pattern-audit",
        PHYSICAL_HOMOTOPY_REQUEST,
        SCHEDULE_CONTINUATION_REQUEST,
    ):
        raise ValueError("unsupported coarse repair schedule endpoint")
    physical_replay = expected_direction_replay_approval is not None
    physical_homotopy = (
        expected_schedule_feasibility_endpoint == PHYSICAL_HOMOTOPY_REQUEST
    )
    direction_continuation = (
        expected_schedule_feasibility_endpoint == SCHEDULE_CONTINUATION_REQUEST
    )
    combined_physical = physical_homotopy or direction_continuation
    if physical_replay != (expected_direction_replay_inputs is not None):
        raise ValueError("physical replay approval/input expectations are incomplete")
    if combined_physical is not (
        expected_physical_schedule_homotopy_endpoint is not None
    ):
        raise ValueError("physical homotopy endpoint expectation is incomplete")
    if combined_physical and not physical_replay:
        raise ValueError("physical homotopy requires fixed-direction approval")
    if (
        physical_replay
        and expected_schedule_feasibility_endpoint is not None
        and not combined_physical
    ):
        raise ValueError("physical replay and schedule search are mutually exclusive")
    if direction_continuation is not (
        expected_schedule_direction_continuation_endpoint is not None
        and expected_homotopy_audit_evidence is not None
    ):
        raise ValueError("schedule direction continuation expectation is incomplete")
    manifest_status = manifest.get("status")
    physical_incomplete = (
        physical_replay
        and allow_physical_incomplete
        and manifest_status == "INCOMPLETE"
    )
    if manifest_status != "PASS" and not physical_incomplete:
        raise ValueError("coarse repair audit manifest is not an accepted result")
    expected_audit_purpose = (
        (
            SCHEDULE_CONTINUATION_PURPOSE
            if direction_continuation
            else (
                PHYSICAL_HOMOTOPY_PURPOSE
                if physical_homotopy
                else "physical_local_schedule_fixed_direction_replay"
            )
        )
        if physical_replay
        else (
            "repair_profile_discovery"
            if expected_schedule_feasibility_endpoint is None
            else (
                "minimum_growth_plus_existing_direction_repair"
                if expected_schedule_feasibility_endpoint
                == "minimum-growth-existing-direction-repair"
                else (
                    "minimum_growth_owner_free_component_direction_audit"
                    if expected_schedule_feasibility_endpoint
                    == "minimum-growth-owner-free-component-direction-audit"
                    else "minimum_growth_owner_free_component_pattern_audit"
                )
            )
        )
    )
    output = expected_output_directory.resolve(strict=True)
    resolved_manifest = manifest_path.resolve(strict=True)
    expected_config = expected_config_path.resolve(strict=True)
    expected_projection_path = Path(
        str(projection_evidence.get("path", ""))
    ).resolve(strict=True)
    expected_schedule_path = Path(
        str(local_schedule_binding.get("source_plan_path", ""))
    ).resolve(strict=True)
    replay_source_paths: tuple[Path, ...] = ()
    homotopy_source_paths: tuple[Path, ...] = ()
    replay_source_inputs: dict[str, Any] | None = None
    if physical_replay:
        replay_binding_sha256 = str(
            expected_direction_replay_approval.get(
                "local_schedule_binding_sha256",
                local_schedule_binding.get("binding_sha256", ""),
            )
            if direction_continuation
            else local_schedule_binding.get("binding_sha256", "")
        )
        quality = contract.get("quality")
        projected = projection_evidence.get("projected_3d_elements")
        if (
            not isinstance(quality, Mapping)
            or isinstance(projected, bool)
            or not isinstance(projected, int)
            or projected <= 0
        ):
            raise ValueError("physical replay quality/projection contract is incomplete")
        try:
            trusted_replay_approval = validate_direction_replay_approval(
                expected_direction_replay_approval,
                expected_coarse_contract_sha256=str(
                    projection_evidence.get("coarse_contract_sha256", "")
                ),
                expected_characteristic_length_m=(
                    expected_characteristic_length_m
                ),
                expected_local_schedule_binding_sha256=str(
                    replay_binding_sha256
                ),
                expected_projection_manifest_sha256=str(
                    projection_evidence.get("sha256", "")
                ),
                expected_projected_3d_elements=projected,
                expected_minimum_prism_scaled_jacobian=float(
                    quality["minimum_prism_scaled_jacobian"]
                ),
                expected_minimum_core_tetra_gamma=float(
                    quality["minimum_core_tetra_gamma"]
                ),
            )
        except (
            CoarseDirectionReplayError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError(
                f"trusted physical replay approval is invalid: {error}"
            ) from error
        if trusted_replay_approval != dict(expected_direction_replay_approval):
            raise ValueError("physical replay approval changed during validation")
        if combined_physical:
            design = contract.get("boundary_layer_design")
            if not isinstance(design, Mapping):
                raise ValueError("physical homotopy design contract is missing")
            try:
                trusted_homotopy_endpoint = (
                    validate_physical_schedule_homotopy_endpoint(
                        expected_physical_schedule_homotopy_endpoint,
                        expected_binding_sha256=str(
                            replay_binding_sha256
                        ),
                        expected_first_layer_height_m=float(
                            design["first_layer_height_m"]
                        ),
                        expected_layer_count=int(design["layer_count"]),
                    )
                )
            except (
                CoarseDirectionReplayError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise ValueError(
                    f"trusted physical homotopy endpoint is invalid: {error}"
                ) from error
            if trusted_homotopy_endpoint != dict(
                expected_physical_schedule_homotopy_endpoint
            ):
                raise ValueError(
                    "physical homotopy endpoint changed during validation"
                )
        if direction_continuation:
            evidence = expected_homotopy_audit_evidence
            homotopy_source_bindings = evidence.get("discovery", {}).get(
                "source_bindings", {}
            )
            endpoint_arguments = {
                "homotopy_manifest_sha256": evidence.get("sha256"),
                "homotopy_discovery": evidence.get("discovery"),
                "homotopy_endpoint_sha256": (
                    expected_physical_schedule_homotopy_endpoint.get(
                        "endpoint_sha256"
                    )
                ),
                "direction_replay_approval_sha256": (
                    homotopy_source_bindings.get(
                        "direction_replay_approval_sha256"
                    )
                ),
                "local_schedule_binding_sha256": (
                    homotopy_source_bindings.get(
                        "local_schedule_binding_sha256"
                    )
                ),
                "minimum_prism_scaled_jacobian_for_pass": float(
                    quality["minimum_prism_scaled_jacobian"]
                ),
                "minimum_core_tetra_gamma_for_pass": float(
                    quality["minimum_core_tetra_gamma"]
                ),
                "layer_count": int(contract["boundary_layer_design"]["layer_count"]),
                "first_layer_height_m": float(
                    contract["boundary_layer_design"]["first_layer_height_m"]
                ),
            }
            try:
                trusted_continuation_endpoint = (
                    validate_schedule_frontier_direction_endpoint(
                        expected_schedule_direction_continuation_endpoint,
                        **endpoint_arguments,
                    )
                )
            except (
                CoarseScheduleContinuationError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise ValueError(
                    f"trusted schedule direction continuation endpoint is invalid: {error}"
                ) from error
            if trusted_continuation_endpoint != dict(
                expected_schedule_direction_continuation_endpoint
            ):
                raise ValueError(
                    "schedule direction continuation endpoint changed during validation"
                )
        expected_input_fields = {
            "primary_path",
            "primary_sha256",
            "confirmation_path",
            "confirmation_sha256",
        }
        if (
            not isinstance(expected_direction_replay_inputs, Mapping)
            or set(expected_direction_replay_inputs) != expected_input_fields
        ):
            raise ValueError("physical replay source input identity is incomplete")
        try:
            primary_path = Path(
                str(expected_direction_replay_inputs["primary_path"])
            ).resolve(strict=True)
            confirmation_path = Path(
                str(expected_direction_replay_inputs["confirmation_path"])
            ).resolve(strict=True)
        except OSError as error:
            raise ValueError("physical replay source manifest is stale") from error
        primary_sha256 = str(
            expected_direction_replay_inputs["primary_sha256"]
        ).casefold()
        confirmation_sha256 = str(
            expected_direction_replay_inputs["confirmation_sha256"]
        ).casefold()
        replay_source_inputs = {
            "primary_path": primary_path,
            "primary_sha256": primary_sha256,
            "confirmation_path": confirmation_path,
            "confirmation_sha256": confirmation_sha256,
        }
        source_identity = {
            (str(Path(str(record["path"])).resolve(strict=True)), record["sha256"])
            for record in trusted_replay_approval["source_audit_manifests"]
        }
        if (
            primary_path == confirmation_path
            or primary_sha256 == confirmation_sha256
            or not _is_lower_sha256(primary_sha256)
            or not _is_lower_sha256(confirmation_sha256)
            or {
                (str(primary_path), primary_sha256),
                (str(confirmation_path), confirmation_sha256),
            }
            != source_identity
        ):
            raise ValueError("physical replay source manifests differ from approval")
        replay_source_paths = (primary_path, confirmation_path)
        if direction_continuation:
            try:
                homotopy_source_path = Path(
                    str(expected_homotopy_audit_evidence["path"])
                ).resolve(strict=True)
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise ValueError(
                    "schedule continuation homotopy source is stale"
                ) from error
            if (
                homotopy_source_path in replay_source_paths
                or homotopy_source_path.parent.parent
                != expected_output_directory.resolve(strict=True).parent
                or _sha256_file(homotopy_source_path)
                != expected_homotopy_audit_evidence.get("sha256")
            ):
                raise ValueError(
                    "schedule continuation homotopy source identity changed"
                )
            homotopy_source_paths = (homotopy_source_path,)
    if (
        resolved_manifest.parent != output
        or manifest_path.is_symlink()
        or any(
            _coarse_repair_path_has_link_component(path)
            for path in (
                output,
                resolved_manifest,
                expected_config,
                expected_projection_path,
                expected_schedule_path,
                *replay_source_paths,
                *homotopy_source_paths,
            )
        )
    ):
        raise ValueError("coarse repair audit manifest is outside its run directory")
    contract_sha256 = str(contract.get("normalized_config_sha256", ""))
    selected = manifest.get("characteristic_length_m")
    if (
        manifest.get("schema") != "cfdpipe.coarse_repair_audit_manifest.v1"
        or manifest.get("status") != manifest_status
        or manifest.get("audit_only") is not True
        or manifest.get("calibration_PASS_authorized") is not False
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("mesh_written") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
        or manifest.get("external_commands") != []
        or manifest.get("error") is not None
        or "audit_purpose" not in manifest
        or "schedule_feasibility_endpoint_requested" not in manifest
        or manifest.get("audit_purpose") != expected_audit_purpose
        or manifest.get("schedule_feasibility_endpoint_requested")
        != expected_schedule_feasibility_endpoint
        or manifest.get("direction_replay_approval")
        != (
            dict(expected_direction_replay_approval)
            if physical_replay
            else None
        )
        or manifest.get("physical_schedule_homotopy_endpoint")
        != (
            dict(expected_physical_schedule_homotopy_endpoint)
            if combined_physical
            else None
        )
        or manifest.get("schedule_direction_continuation_endpoint")
        != (
            dict(expected_schedule_direction_continuation_endpoint)
            if direction_continuation
            else None
        )
        or manifest.get("homotopy_audit_source")
        != (
            {
                key: expected_homotopy_audit_evidence.get(key)
                for key in (
                    "path",
                    "sha256",
                    "size_bytes",
                    "manifest_status",
                )
            }
            if direction_continuation
            else None
        )
        or manifest.get("source_unchanged") is not True
        or manifest.get("coarse_contract_sha256") != contract_sha256
        or isinstance(selected, bool)
        or not isinstance(selected, (int, float))
        or not math.isfinite(float(selected))
        or not math.isclose(
            float(selected),
            expected_characteristic_length_m,
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
    ):
        raise ValueError("coarse repair audit manifest header is not a strict PASS")

    started_at = _parse_canonical_utc_z(
        manifest.get("started_at_utc"), "coarse repair audit start"
    )
    ended_at = _parse_canonical_utc_z(
        manifest.get("ended_at_utc"), "coarse repair audit end"
    )
    elapsed_seconds = manifest.get("elapsed_seconds")
    gmsh_version = manifest.get("gmsh_version")
    gmsh_module_value = manifest.get("gmsh_module_path")
    if (
        ended_at < started_at
        or isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or not math.isfinite(float(elapsed_seconds))
        or float(elapsed_seconds) <= 0.0
        or not isinstance(gmsh_version, str)
        or not gmsh_version.strip()
        or gmsh_version.casefold() == "unknown"
        or not isinstance(gmsh_module_value, str)
    ):
        raise ValueError("coarse repair audit timing/Gmsh identity is incomplete")
    try:
        gmsh_module_path = Path(gmsh_module_value).resolve(strict=True)
    except OSError as error:
        raise ValueError("coarse repair audit Gmsh module path is stale") from error
    if (
        not gmsh_module_path.is_file()
        or _coarse_repair_path_has_link_component(gmsh_module_path)
    ):
        raise ValueError("coarse repair audit Gmsh module path is unsafe")

    source_before = manifest.get("source_before")
    source_after = manifest.get("source_after")
    try:
        expected_source = Path(str(contract.get("pipeline_brep_path", ""))).resolve(
            strict=True
        )
    except OSError as error:
        raise ValueError("coarse repair audit source BREP no longer exists") from error
    expected_source_sha256 = contract.get("provenance", {}).get(
        "pipeline_brep_sha256"
    )
    if (
        not isinstance(source_before, Mapping)
        or not isinstance(source_after, Mapping)
        or dict(source_before) != dict(source_after)
        or not _same_resolved_path(source_before.get("path"), expected_source)
        or source_before.get("sha256") != expected_source_sha256
        or source_before.get("read_only") is not True
        or isinstance(source_before.get("size_bytes"), bool)
        or not isinstance(source_before.get("size_bytes"), int)
        or source_before["size_bytes"] <= 0
        or _coarse_repair_path_has_link_component(expected_source)
        or not expected_source.is_file()
        or expected_source.suffix.casefold() != ".brep"
        or expected_source.stat().st_size != source_before["size_bytes"]
        or _sha256_file(expected_source) != expected_source_sha256
        or bool(expected_source.stat().st_mode & stat.S_IWUSR)
    ):
        raise ValueError("coarse repair audit source BREP evidence is stale")

    projection_size = projection_evidence.get("size_bytes")
    if (
        _sha256_file(expected_config) != expected_config_sha256
        or _sha256_file(expected_projection_path)
        != projection_evidence.get("sha256")
        or isinstance(projection_size, bool)
        or not isinstance(projection_size, int)
        or projection_size <= 0
        or expected_projection_path.stat().st_size != projection_size
    ):
        raise ValueError("coarse repair audit config/projection evidence is stale")
    if physical_replay:
        approval_sources = {
            str(Path(str(record["path"])).resolve(strict=True)): record
            for record in expected_direction_replay_approval[
                "source_audit_manifests"
            ]
        }
        for source_path in replay_source_paths:
            source_record = approval_sources.get(str(source_path))
            if (
                source_record is None
                or not source_path.is_file()
                or source_path.is_symlink()
                or source_path.name != "coarse_repair_audit_manifest.json"
                or source_path.stat().st_size != source_record.get("size_bytes")
                or _sha256_file(source_path) != source_record.get("sha256")
            ):
                raise ValueError("physical replay source audit evidence is stale")

    session = manifest.get("gmsh_session")
    diagnostic = session.get("diagnostic_audit") if isinstance(session, Mapping) else None
    write_guard = session.get("write_guard") if isinstance(session, Mapping) else None
    if (
        not isinstance(session, Mapping)
        or session.get("initialize_attempted") is not True
        or session.get("initialize_called") is not True
        or session.get("logger_start_attempted") is not True
        or session.get("logger_started") is not True
        or session.get("logger_get_attempted") is not True
        or session.get("logger_capture_succeeded") is not True
        or not isinstance(session.get("logger_messages"), list)
        or any(not isinstance(value, str) for value in session["logger_messages"])
        or session.get("logger_stop_attempted") is not True
        or session.get("logger_stopped") is not True
        or session.get("clear_called") is not True
        or session.get("finalize_attempted") is not True
        or session.get("finalize_called") is not True
        or session.get("cleanup_errors") != []
        or not isinstance(diagnostic, Mapping)
        or diagnostic.get("status") != "PASS"
        or diagnostic.get("raw_messages_preserved") is not True
        or diagnostic.get("fatal_messages") != []
        or diagnostic.get("pending_diagnostics") != []
        or not isinstance(diagnostic.get("classified_nonfatal_diagnostics"), list)
        or not isinstance(diagnostic.get("associated_summary_lines"), list)
        or not isinstance(diagnostic.get("policy"), str)
        or not diagnostic["policy"]
        or not isinstance(write_guard, Mapping)
        or write_guard.get("installed") is not True
        or write_guard.get("write_attempt_count") != 0
        or write_guard.get("blocked_paths") != []
    ):
        raise ValueError("coarse repair audit Gmsh lifecycle is incomplete")

    run_evidence = manifest.get("run_evidence")
    expected_run_evidence_keys = {
        "worker_argv",
        "config_path",
        "config_sha256",
        "coarse_contract_sha256",
    }
    if expected_schedule_feasibility_endpoint is not None:
        expected_run_evidence_keys.add("schedule_feasibility_endpoint")
    if physical_replay:
        expected_run_evidence_keys.update(
            {
                "direction_audit_primary_path",
                "direction_audit_primary_sha256",
                "direction_audit_confirmation_path",
                "direction_audit_confirmation_sha256",
                "direction_replay_approval_sha256",
            }
        )
    if combined_physical:
        expected_run_evidence_keys.add(
            "physical_schedule_homotopy_endpoint_sha256"
        )
    if direction_continuation:
        expected_run_evidence_keys.update(
            {
                "homotopy_audit_manifest_path",
                "homotopy_audit_manifest_sha256",
                "schedule_direction_continuation_endpoint_sha256",
            }
        )
    if (
        not isinstance(run_evidence, Mapping)
        or set(run_evidence) != expected_run_evidence_keys
        or run_evidence.get("config_sha256") != expected_config_sha256
        or run_evidence.get("coarse_contract_sha256") != contract_sha256
        or not _same_resolved_path(run_evidence.get("config_path"), expected_config)
        or (
            expected_schedule_feasibility_endpoint is not None
            and run_evidence.get("schedule_feasibility_endpoint")
            != expected_schedule_feasibility_endpoint
        )
        or (
            physical_replay
            and (
                not _same_resolved_path(
                    run_evidence.get("direction_audit_primary_path"),
                    replay_source_inputs["primary_path"],
                )
                or run_evidence.get("direction_audit_primary_sha256")
                != replay_source_inputs["primary_sha256"]
                or not _same_resolved_path(
                    run_evidence.get("direction_audit_confirmation_path"),
                    replay_source_inputs["confirmation_path"],
                )
                or run_evidence.get("direction_audit_confirmation_sha256")
                != replay_source_inputs["confirmation_sha256"]
                or run_evidence.get("direction_replay_approval_sha256")
                != expected_direction_replay_approval.get("approval_sha256")
                or (
                    combined_physical
                    and run_evidence.get(
                        "physical_schedule_homotopy_endpoint_sha256"
                    )
                    != expected_physical_schedule_homotopy_endpoint.get(
                        "endpoint_sha256"
                    )
                )
                or (
                    direction_continuation
                    and (
                        not _same_resolved_path(
                            run_evidence.get(
                                "homotopy_audit_manifest_path"
                            ),
                            homotopy_source_paths[0],
                        )
                        or run_evidence.get(
                            "homotopy_audit_manifest_sha256"
                        )
                        != expected_homotopy_audit_evidence.get("sha256")
                        or run_evidence.get(
                            "schedule_direction_continuation_endpoint_sha256"
                        )
                        != expected_schedule_direction_continuation_endpoint.get(
                            "endpoint_sha256"
                        )
                    )
                )
            )
        )
    ):
        raise ValueError("coarse repair audit run evidence is incomplete")

    projected_count = projection_evidence.get("projected_3d_elements")
    try:
        embedded_projection = validate_projection_evidence_record(
            manifest.get("projection_evidence"),
            expected_path=str(projection_evidence.get("path", "")),
            expected_sha256=str(projection_evidence.get("sha256", "")),
            expected_config_sha256=expected_config_sha256,
            expected_coarse_contract_sha256=str(
                projection_evidence.get("coarse_contract_sha256", "")
            ),
            expected_characteristic_length_m=expected_characteristic_length_m,
            expected_projected_3d_elements=projected_count,
        )
    except CoarseProjectionEvidenceError as error:
        raise ValueError(
            f"coarse repair audit projection lineage is invalid: {error}"
        ) from error
    if embedded_projection.get("evidence_sha256") != projection_evidence.get(
        "evidence_sha256"
    ):
        raise ValueError("coarse repair audit projection lineage changed")

    embedded_schedule = manifest.get("local_schedule_binding")
    if (
        not isinstance(embedded_schedule, Mapping)
        or embedded_schedule.get("schema")
        != "cfdpipe.boundary_layer_local_schedule_binding.v1"
        or embedded_schedule.get("status") != "PASS"
        or embedded_schedule.get("coarse_contract_sha256")
        != projection_evidence.get("coarse_contract_sha256")
        or embedded_schedule.get("binding_sha256")
        != local_schedule_binding.get("binding_sha256")
        or embedded_schedule.get("source_plan_path")
        != local_schedule_binding.get("source_plan_path")
        or embedded_schedule.get("source_plan_sha256")
        != local_schedule_binding.get("source_plan_sha256")
    ):
        raise ValueError("coarse repair audit local-schedule lineage is incomplete")

    strategy = manifest.get("strategy_config")
    strategy_sha256 = manifest.get("strategy_config_sha256")
    if not isinstance(strategy, Mapping) or not _is_lower_sha256(strategy_sha256):
        raise ValueError("coarse repair audit strategy identity is incomplete")
    unsigned_strategy = dict(strategy)
    unsigned_strategy.pop("normalized_config_sha256", None)
    if (
        strategy.get("normalized_config_sha256") != strategy_sha256
        or _canonical_mapping_sha256(unsigned_strategy) != strategy_sha256
        or strategy.get("repair_audit_only") is not True
        or strategy.get("contract_mode") != "coarse_repair_audit_only"
        or strategy.get("calibration_PASS_authorized") is not False
        or strategy.get("production_mesh_eligible") is not False
        or strategy.get("mesh_written") is not False
    ):
        raise ValueError("coarse repair audit strategy scope/hash is invalid")
    if (
        isinstance(contract.get("base_smoke_contract"), Mapping)
        and isinstance(contract.get("post_mesh_repair"), Mapping)
    ):
        try:
            expected_strategy = make_coarse_repair_audit_strategy_config(
                contract,
                expected_characteristic_length_m,
                local_schedule_binding=local_schedule_binding,
                schedule_feasibility_endpoint=(
                    expected_schedule_feasibility_endpoint
                ),
                direction_replay_approval=(
                    expected_direction_replay_approval
                ),
                homotopy_audit_evidence=(
                    expected_homotopy_audit_evidence
                ),
            )
        except (CoarseRepairAuditError, CoarseMeshError) as error:
            raise ValueError(
                "trusted coarse repair strategy could not be reconstructed"
            ) from error
        if dict(strategy) != expected_strategy:
            raise ValueError(
                "coarse repair audit strategy differs from the trusted deterministic contract"
            )
    raw_endpoint = strategy.get("schedule_feasibility_endpoint")
    if physical_replay:
        if (
            (
                "schedule_feasibility_endpoint" in strategy
                and not combined_physical
            )
            or "owner_free_direction_endpoint" in strategy
            or strategy.get("audit_purpose") != expected_audit_purpose
            or strategy.get("fixed_direction_replay_only") is not True
            or strategy.get("combined_endpoint_only")
            is not bool(combined_physical)
            or strategy.get("owner_free_direction_replay_approval")
            != dict(expected_direction_replay_approval)
        ):
            raise ValueError(
                "physical replay strategy scope/approval is incomplete"
            )
        if combined_physical:
            if (
                strategy.get("physical_schedule_homotopy_endpoint")
                != dict(expected_physical_schedule_homotopy_endpoint)
                or raw_endpoint
                != dict(expected_physical_schedule_homotopy_endpoint)
            ):
                raise ValueError(
                    "physical homotopy strategy endpoint is incomplete"
                )
            if physical_homotopy and (
                strategy.get("fixed_direction_homotopy_only") is not True
                or "first_frontier_direction_continuation_only" in strategy
                or "schedule_direction_continuation_endpoint" in strategy
            ):
                raise ValueError(
                    "physical homotopy strategy scope is incomplete"
                )
            if direction_continuation and (
                strategy.get(
                    "first_frontier_direction_continuation_only"
                )
                is not True
                or "fixed_direction_homotopy_only" in strategy
                or strategy.get("schedule_direction_continuation_endpoint")
                != dict(expected_schedule_direction_continuation_endpoint)
            ):
                raise ValueError(
                    "schedule direction continuation strategy endpoint is incomplete"
                )
        elif (
            "fixed_direction_homotopy_only" in strategy
            or "physical_schedule_homotopy_endpoint" in strategy
            or "first_frontier_direction_continuation_only" in strategy
            or "schedule_direction_continuation_endpoint" in strategy
        ):
            raise ValueError("ordinary physical replay contains homotopy state")
    elif expected_schedule_feasibility_endpoint is None:
        if (
            "schedule_feasibility_endpoint" in strategy
            or "audit_purpose" in strategy
            or "combined_endpoint_only" in strategy
            or "owner_free_direction_endpoint" in strategy
            or "fixed_direction_replay_only" in strategy
            or "owner_free_direction_replay_approval" in strategy
            or "fixed_direction_homotopy_only" in strategy
            or "physical_schedule_homotopy_endpoint" in strategy
            or "first_frontier_direction_continuation_only" in strategy
            or "schedule_direction_continuation_endpoint" in strategy
        ):
            raise ValueError(
                "ordinary coarse repair audit contains a schedule endpoint"
            )
    else:
        design = contract.get("boundary_layer_design")
        if not isinstance(design, Mapping):
            raise ValueError("coarse contract lacks boundary-layer design")
        try:
            validate_minimum_growth_endpoint(
                raw_endpoint,
                expected_binding_sha256=str(
                    local_schedule_binding.get("binding_sha256", "")
                ),
                expected_first_layer_height_m=float(
                    design.get("first_layer_height_m")
                ),
                expected_layer_count=int(design.get("layer_count")),
            )
        except (CoarseScheduleFeasibilityError, TypeError, ValueError) as error:
            raise ValueError(
                f"coarse repair schedule endpoint is invalid: {error}"
            ) from error
        if (
            strategy.get("audit_purpose") != expected_audit_purpose
            or strategy.get("combined_endpoint_only") is not True
        ):
            raise ValueError(
                "coarse repair combined endpoint scope is incomplete"
            )
        raw_direction_endpoint = strategy.get(
            "owner_free_direction_endpoint"
        )
        if (
            expected_schedule_feasibility_endpoint
            == "minimum-growth-existing-direction-repair"
        ):
            if "owner_free_direction_endpoint" in strategy:
                raise ValueError(
                    "existing-direction audit contains an owner-free endpoint"
                )
        else:
            try:
                validated_direction_endpoint = validate_owner_free_direction_endpoint(
                    raw_direction_endpoint,
                    expected_schedule_endpoint_sha256=str(
                        raw_endpoint.get("endpoint_sha256", "")
                        if isinstance(raw_endpoint, Mapping)
                        else ""
                    ),
                )
            except CoarseDirectionFeasibilityError as error:
                raise ValueError(
                    f"coarse owner-free direction endpoint is invalid: {error}"
                ) from error
            expected_direction_schema = (
                OWNER_FREE_DIRECTION_ENDPOINT_SCHEMA
                if expected_schedule_feasibility_endpoint
                == "minimum-growth-owner-free-component-direction-audit"
                else OWNER_FREE_PATTERN_ENDPOINT_SCHEMA
            )
            if (
                validated_direction_endpoint.get("schema")
                != expected_direction_schema
            ):
                raise ValueError(
                    "coarse owner-free endpoint schema does not match the requested audit"
                )

        strategy_evidence = manifest.get("strategy_evidence")
        post_mesh_repair = (
            strategy_evidence.get("post_mesh_repair")
            if isinstance(strategy_evidence, Mapping)
            else None
        )
        subdivision = (
            post_mesh_repair.get("subdivision")
            if isinstance(post_mesh_repair, Mapping)
            else None
        )
        applied_binding = (
            subdivision.get("surface_schedule_binding")
            if isinstance(subdivision, Mapping)
            else None
        )
        raw_baseline_schedules = local_schedule_binding.get(
            "surface_schedules"
        )
        baseline_schedules: dict[str, Any] | None = None
        if isinstance(raw_baseline_schedules, Mapping):
            baseline_schedules = {}
            for fingerprint, raw_schedule in raw_baseline_schedules.items():
                if isinstance(raw_schedule, Mapping):
                    raw_schedule = raw_schedule.get("cumulative_heights_m")
                if (
                    not isinstance(raw_schedule, (list, tuple))
                    or isinstance(raw_schedule, (str, bytes))
                ):
                    raise ValueError(
                        "coarse repair baseline schedule record lacks cumulative heights"
                    )
                baseline_schedules[str(fingerprint)] = list(raw_schedule)
        wall_fingerprints = contract.get("wall_surface_fingerprints")
        if (
            not isinstance(applied_binding, Mapping)
            or applied_binding.get("schedule_feasibility_endpoint")
            != raw_endpoint
            or not isinstance(baseline_schedules, Mapping)
            or not isinstance(wall_fingerprints, list)
        ):
            raise ValueError(
                "coarse repair schedule endpoint has no runtime application evidence"
            )
        try:
            validate_minimum_growth_application(
                applied_binding.get("schedule_feasibility_application"),
                endpoint=raw_endpoint,
                baseline_surface_schedules=baseline_schedules,
                expected_surface_fingerprints=wall_fingerprints,
            )
        except CoarseScheduleFeasibilityError as error:
            raise ValueError(
                f"coarse repair schedule application is invalid: {error}"
            ) from error

    discovery = manifest.get("repair_discovery")
    direction_audit = expected_schedule_feasibility_endpoint in (
        "minimum-growth-owner-free-component-direction-audit",
        "minimum-growth-owner-free-component-pattern-audit",
    )
    signature: Mapping[str, Any] | None = None
    if physical_replay:
        source_quality = expected_direction_replay_approval.get(
            "source_minimum_growth_final_quality"
        )
        design = contract.get("boundary_layer_design")
        quality = contract.get("quality")
        walls = contract.get("wall_surface_fingerprints")
        if (
            not isinstance(source_quality, Mapping)
            or not isinstance(design, Mapping)
            or not isinstance(quality, Mapping)
            or not isinstance(walls, list)
        ):
            raise ValueError("physical replay validation contract is incomplete")
        try:
            expected_layer_count = int(design["layer_count"])
            expected_surface_count = len(walls)
            expected_surface_schedules = _physical_surface_schedule_values(
                local_schedule_binding,
                expected_surface_count=expected_surface_count,
                expected_layer_count=expected_layer_count,
            )
            physical_validator = (
                validate_schedule_frontier_direction_continuation
                if direction_continuation
                else (
                    validate_physical_schedule_homotopy_discovery
                    if physical_homotopy
                    else validate_physical_schedule_replay_discovery
                )
            )
            physical_validator_arguments: dict[str, Any] = {
                "expected_approval": expected_direction_replay_approval,
                "expected_projected_3d_elements": int(projected_count),
                "expected_prism_element_count": int(
                    source_quality["prism_element_count"]
                ),
                "expected_core_element_count": int(
                    source_quality["core_element_count"]
                ),
                "expected_layer_count": expected_layer_count,
                "expected_surface_count": expected_surface_count,
                "expected_surface_schedules": expected_surface_schedules,
                "expected_minimum_prism_scaled_jacobian": float(
                    quality["minimum_prism_scaled_jacobian"]
                ),
                "expected_minimum_core_tetra_gamma": float(
                    quality["minimum_core_tetra_gamma"]
                ),
            }
            if direction_continuation:
                runtime_counts = discovery.get("observed_counts", {})
                runtime_prism_count = int(
                    runtime_counts["prism_element_count"]
                )
                runtime_core_count = int(
                    runtime_counts["core_element_count"]
                )
                if runtime_prism_count != int(
                    source_quality["prism_element_count"]
                ):
                    raise ValueError(
                        "continuation changed the frozen Prism6 inventory"
                    )
                physical_validator_arguments = {
                    "expected_endpoint": (
                        expected_schedule_direction_continuation_endpoint
                    ),
                    "expected_projected_3d_elements": (
                        runtime_prism_count + runtime_core_count
                    ),
                    "expected_prism_element_count": runtime_prism_count,
                    "expected_core_element_count": runtime_core_count,
                    "expected_historical_core_element_count": int(
                        source_quality["core_element_count"]
                    ),
                }
            elif physical_homotopy:
                physical_validator_arguments["expected_endpoint"] = (
                    expected_physical_schedule_homotopy_endpoint
                )
            strictly_validated_discovery = physical_validator(
                discovery, **physical_validator_arguments
            )
        except (
            CoarseDirectionReplayError,
            CoarseScheduleContinuationError,
            CoarseRepairAuditError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError(
                "physical-schedule replay discovery failed its authoritative "
                f"schema gate: {error}"
            ) from error
        counts = discovery.get("observed_counts")
        if (
            not isinstance(discovery, Mapping)
            or discovery.get("schema")
            != (
                SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA
                if direction_continuation
                else (
                    HOMOTOPY_DISCOVERY_SCHEMA
                    if physical_homotopy
                    else PHYSICAL_DISCOVERY_SCHEMA
                )
            )
            or discovery.get("status") != manifest_status
            or discovery.get("profile_complete")
            is not (manifest_status == "PASS")
            or not _is_lower_sha256(discovery.get("discovery_sha256"))
            or not isinstance(counts, Mapping)
            or counts.get("projected_3d_element_count")
            != (
                runtime_prism_count + runtime_core_count
                if direction_continuation
                else projected_count
            )
        ):
            raise ValueError(
                "physical-schedule replay discovery is incomplete or stale"
            )
    elif direction_audit:
        try:
            strictly_validated_discovery = validate_component_direction_discovery(
                discovery,
                expected_direction_endpoint=strategy[
                    "owner_free_direction_endpoint"
                ],
            )
        except (CoarseComponentDirectionError, KeyError) as error:
            raise ValueError(
                "coarse owner-free direction discovery failed its authoritative "
                f"schema gate: {error}"
            ) from error
        signature = (
            discovery.get("stable_observation")
            if isinstance(discovery, Mapping)
            else None
        )
        counts = (
            discovery.get("observed_counts")
            if isinstance(discovery, Mapping)
            else None
        )
        final_quality = (
            discovery.get("final_quality")
            if isinstance(discovery, Mapping)
            else None
        )
        search = (
            discovery.get("search") if isinstance(discovery, Mapping) else None
        )
        if (
            not isinstance(discovery, Mapping)
            or discovery.get("schema")
            != "cfdpipe.coarse_component_direction_discovery.v1"
            or discovery.get("status") != "PASS"
            or discovery.get("profile_complete") is not True
            or discovery.get("direction_endpoint")
            != strategy.get("owner_free_direction_endpoint")
            or not isinstance(signature, Mapping)
            or not _is_lower_sha256(
                discovery.get("stable_observation_sha256")
            )
            or _canonical_mapping_sha256(signature)
            != discovery.get("stable_observation_sha256")
            or not isinstance(counts, Mapping)
            or counts.get("projected_3d_element_count") != projected_count
            or any(
                counts.get(field) != 0
                for field in (
                    "final_nonpositive_element_count",
                    "final_prism_below_threshold_count",
                    "final_core_tetra_below_gamma_count",
                    "final_nonfinite_count",
                )
            )
            or not isinstance(final_quality, Mapping)
            or final_quality.get("status") != "PASS"
            or not isinstance(search, Mapping)
            or search.get("status") != "PASS"
            or search.get("sharp_owner_used") is not False
            or search.get("absolute_coordinate_replay") is not True
            or search.get("aba_replay_status") != "PASS"
            or search.get("monotonicity_assumed") is not False
        ):
            raise ValueError(
                "coarse owner-free direction discovery is incomplete or stale"
            )
    else:
        try:
            strictly_validated_discovery = validate_coarse_repair_discovery(
                discovery
            )
        except CoarseRepairAuditError as error:
            raise ValueError(
                "coarse repair discovery failed its authoritative schema gate: "
                f"{error}"
            ) from error
        signature = (
            discovery.get("stable_signature")
            if isinstance(discovery, Mapping)
            else None
        )
        counts = (
            discovery.get("observed_counts")
            if isinstance(discovery, Mapping)
            else None
        )
        if (
            not isinstance(discovery, Mapping)
            or discovery.get("schema") != "cfdpipe.coarse_repair_discovery.v1"
            or discovery.get("status") != "PASS"
            or discovery.get("profile_complete") is not True
            or discovery.get("audit_only") is not True
            or discovery.get("calibration_PASS_authorized") is not False
            or discovery.get("production_mesh_eligible") is not False
            or discovery.get("mesh_written") is not False
            or discovery.get("owner_ambiguities") != []
            or not isinstance(signature, Mapping)
            or not _is_lower_sha256(discovery.get("stable_signature_sha256"))
            or _canonical_mapping_sha256(signature)
            != discovery.get("stable_signature_sha256")
            or not isinstance(counts, Mapping)
            or counts != signature.get("observed_counts")
            or counts.get("projected_3d_element_count") != projected_count
            or counts.get("owner_ambiguity_count") != 0
        ):
            raise ValueError("coarse repair discovery is incomplete or stale")
    if strictly_validated_discovery != discovery:
        raise ValueError("coarse repair discovery changed during strict validation")
    if not physical_replay:
        wall_inventory = signature.get("wall_surface_fingerprint_inventory")
        expected_wall_inventory = sorted(
            str(value).casefold()
            for value in contract.get("wall_surface_fingerprints", [])
        )
        if (
            not isinstance(wall_inventory, list)
            or len(wall_inventory) != 48
            or len(set(wall_inventory)) != 48
            or wall_inventory != sorted(wall_inventory)
            or any(not _is_lower_sha256(value) for value in wall_inventory)
            or wall_inventory != expected_wall_inventory
        ):
            raise ValueError("coarse repair discovery lost the 48-wall inventory")

    def reject_runtime_signature_keys(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                lowered = str(key).casefold()
                if lowered.endswith("_tag") or lowered.endswith("_tags") or (
                    "audit" in lowered
                ):
                    raise ValueError(
                        "runtime/audit tags are forbidden in the stable signature"
                    )
                reject_runtime_signature_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_runtime_signature_keys(nested)

    if signature is not None:
        reject_runtime_signature_keys(signature)

    isolation = manifest.get("worker_isolation")
    worker = isolation.get("worker") if isinstance(isolation, Mapping) else None
    memory = (
        isolation.get("worker_memory") if isinstance(isolation, Mapping) else None
    )
    hard_limit = memory.get("hard_limit") if isinstance(memory, Mapping) else None
    job = hard_limit.get("job_object") if isinstance(hard_limit, Mapping) else None
    isolated_projection = (
        isolation.get("projection_evidence")
        if isinstance(isolation, Mapping)
        else None
    )
    isolated_schedule = (
        isolation.get("local_schedule") if isinstance(isolation, Mapping) else None
    )
    pre_job_schedule = (
        isolated_schedule.get("pre_job_validation")
        if isinstance(isolated_schedule, Mapping)
        else None
    )
    isolated_binding = (
        isolated_schedule.get("binding")
        if isinstance(isolated_schedule, Mapping)
        else None
    )
    calibration_limit = contract.get("resource", {}).get(
        "calibration_worker_memory_limit_bytes"
    )
    evidence_path = (
        output.parent
        / "_worker_evidence"
        / output.name
        / "repair_audit_worker.json"
    )
    expected_argv = [
        str(Path(sys.executable).resolve(strict=True)),
        "-m",
        "cfdpipe.coarse_repair_audit_worker",
        "--config",
        str(expected_config),
        "--config-sha256",
        expected_config_sha256,
        "--coarse-contract-sha256",
        contract_sha256,
        "--characteristic-length",
        format(expected_characteristic_length_m, ".17g"),
        "--projection-manifest",
        str(expected_projection_path),
        "--projection-sha256",
        str(projection_evidence.get("sha256", "")),
        "--local-schedule",
        str(expected_schedule_path),
        "--local-schedule-sha256",
        str(local_schedule_binding.get("source_plan_sha256", "")),
    ]
    if expected_schedule_feasibility_endpoint is not None:
        expected_argv.extend(
            [
                "--schedule-feasibility-endpoint",
                expected_schedule_feasibility_endpoint,
            ]
        )
    if physical_replay:
        expected_argv.extend(
            [
                "--direction-audit-primary",
                str(replay_source_inputs["primary_path"]),
                "--direction-audit-primary-sha256",
                replay_source_inputs["primary_sha256"],
                "--direction-audit-confirmation",
                str(replay_source_inputs["confirmation_path"]),
                "--direction-audit-confirmation-sha256",
                replay_source_inputs["confirmation_sha256"],
                "--direction-replay-approval-sha256",
                str(expected_direction_replay_approval["approval_sha256"]),
            ]
        )
        if combined_physical:
            expected_argv.extend(
                [
                    "--physical-schedule-homotopy-endpoint-sha256",
                    str(
                        expected_physical_schedule_homotopy_endpoint[
                            "endpoint_sha256"
                        ]
                    ),
                ]
            )
        if direction_continuation:
            expected_argv.extend(
                [
                    "--homotopy-audit-manifest",
                    str(homotopy_source_paths[0]),
                    "--homotopy-audit-manifest-sha256",
                    str(expected_homotopy_audit_evidence["sha256"]),
                    "--schedule-direction-continuation-endpoint-sha256",
                    str(
                        expected_schedule_direction_continuation_endpoint[
                            "endpoint_sha256"
                        ]
                    ),
                ]
            )
    expected_argv.extend(["--output", str(output)])
    if run_evidence.get("worker_argv") != expected_argv:
        raise ValueError("coarse repair audit run argv differs from the worker")
    required_worker_flags = (
        "hard_limit_installed_before_heavy_import",
        "heavy_dependencies_imported",
        "normalized_resource_matches_raw",
        "local_schedule_bound_after_heavy_import",
        "audit_runner_called",
    )
    expected_isolation_keys = {
        "schema",
        "status",
        "audit_only",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_written",
        "su2_called",
        "paraview_called",
        "started_at_utc",
        "ended_at_utc",
        "config_sha256",
        "coarse_contract_sha256",
        "projection_evidence",
        "local_schedule",
        "direction_replay",
        "worker",
        "worker_memory",
        "audit_result_status",
        "worker_error",
    }
    expected_worker_keys = {
        "argv",
        "config_path",
        "output_directory",
        "manifest_path",
        "evidence_path",
        "characteristic_length_m",
        "configured_process_memory_limit_bytes",
        *required_worker_flags,
    }
    expected_memory_keys = {
        "system_before_limit",
        "process_before_limit",
        "hard_limit",
        "system_after_limit",
        "process_after_limit",
        "system_after_audit",
        "process_after_audit",
    }
    isolated_replay = (
        isolation.get("direction_replay")
        if isinstance(isolation, Mapping)
        else None
    )
    expected_pre_job_sources = None
    expected_isolation_approval = None
    expected_isolation_homotopy_endpoint = None
    expected_isolation_homotopy_source = None
    expected_isolation_continuation_endpoint = None
    if physical_replay:
        expected_pre_job_sources = [
            {
                "path": str(replay_source_inputs["primary_path"]),
                "sha256": replay_source_inputs["primary_sha256"],
                "size_bytes": replay_source_inputs["primary_path"].stat().st_size,
            },
            {
                "path": str(replay_source_inputs["confirmation_path"]),
                "sha256": replay_source_inputs["confirmation_sha256"],
                "size_bytes": replay_source_inputs[
                    "confirmation_path"
                ].stat().st_size,
            },
        ]
        expected_isolation_approval = dict(expected_direction_replay_approval)
        if combined_physical:
            expected_isolation_homotopy_endpoint = dict(
                expected_physical_schedule_homotopy_endpoint
            )
        if direction_continuation:
            expected_isolation_homotopy_source = {
                key: expected_homotopy_audit_evidence[key]
                for key in (
                    "path",
                    "sha256",
                    "size_bytes",
                    "manifest_status",
                )
            }
            expected_isolation_continuation_endpoint = dict(
                expected_schedule_direction_continuation_endpoint
            )
    if (
        not isinstance(isolation, Mapping)
        or set(isolation) != expected_isolation_keys
        or isolation.get("schema") != "cfdpipe.coarse_repair_audit_worker.v1"
        or isolation.get("status") != "PASS"
        or isolation.get("audit_only") is not True
        or isolation.get("calibration_PASS_authorized") is not False
        or isolation.get("production_mesh_eligible") is not False
        or isolation.get("mesh_written") is not False
        or isolation.get("su2_called") is not False
        or isolation.get("paraview_called") is not False
        or isolation.get("config_sha256") != expected_config_sha256
        or isolation.get("coarse_contract_sha256") != contract_sha256
        or isolation.get("audit_result_status") != manifest_status
        or isolation.get("worker_error") is not None
        or not isinstance(worker, Mapping)
        or set(worker) != expected_worker_keys
        or any(worker.get(flag) is not True for flag in required_worker_flags)
        or worker.get("argv") != expected_argv
        or not _same_resolved_path(worker.get("config_path"), expected_config)
        or not _same_resolved_path(worker.get("output_directory"), output)
        or not _same_resolved_path(worker.get("manifest_path"), resolved_manifest)
        or not _same_resolved_path(worker.get("evidence_path"), evidence_path)
        or isinstance(worker.get("characteristic_length_m"), bool)
        or not isinstance(worker.get("characteristic_length_m"), (int, float))
        or not math.isclose(
            float(worker["characteristic_length_m"]),
            expected_characteristic_length_m,
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
        or worker.get("configured_process_memory_limit_bytes")
        != calibration_limit
        or not isinstance(memory, Mapping)
        or set(memory) != expected_memory_keys
        or not isinstance(hard_limit, Mapping)
        or hard_limit.get("status") != "PASS"
        or hard_limit.get("requested_process_memory_limit_bytes")
        != calibration_limit
        or not isinstance(job, Mapping)
        or job.get("process_memory_limit_bytes") != calibration_limit
        or job.get("process_assigned") is not True
        or job.get("handle_retained_for_process_lifetime") is not True
        or any(
            not isinstance(memory.get(name), Mapping)
            or memory[name].get("status") != "PASS"
            for name in (
                "system_before_limit",
                "process_before_limit",
                "system_after_limit",
                "process_after_limit",
                "system_after_audit",
                "process_after_audit",
            )
        )
        or not isinstance(isolated_projection, Mapping)
        or dict(isolated_projection) != dict(projection_evidence)
        or not isinstance(pre_job_schedule, Mapping)
        or not _same_resolved_path(
            pre_job_schedule.get("path"), expected_schedule_path
        )
        or pre_job_schedule.get("sha256")
        != local_schedule_binding.get("source_plan_sha256")
        or pre_job_schedule.get("size_bytes")
        != local_schedule_binding.get("source_plan_size_bytes")
        or pre_job_schedule.get("validated_before_job_object") is not True
        or pre_job_schedule.get("regular_non_link_json") is not True
        or pre_job_schedule.get("inside_runs_mesh_coarse") is not True
        or pre_job_schedule.get("outside_current_output_and_evidence") is not True
        or not isinstance(isolated_binding, Mapping)
        or isolated_binding.get("schema")
        != "cfdpipe.boundary_layer_local_schedule_binding.v1"
        or isolated_binding.get("status") != "PASS"
        or isolated_binding.get("coarse_contract_sha256")
        != projection_evidence.get("coarse_contract_sha256")
        or isolated_binding.get("binding_sha256")
        != local_schedule_binding.get("binding_sha256")
        or isolated_binding.get("source_plan_path")
        != local_schedule_binding.get("source_plan_path")
        or isolated_binding.get("source_plan_sha256")
        != local_schedule_binding.get("source_plan_sha256")
        or isolated_binding.get("source_plan_size_bytes")
        != local_schedule_binding.get("source_plan_size_bytes")
        or isolated_binding.get("surface_count")
        != local_schedule_binding.get("surface_count")
        or isolated_binding.get("layer_count")
        != local_schedule_binding.get("layer_count")
        or not isinstance(isolated_replay, Mapping)
        or set(isolated_replay)
        != {
            "pre_job_sources",
            "approval",
            "homotopy_endpoint",
            "homotopy_audit_source",
            "schedule_direction_continuation_endpoint",
        }
        or isolated_replay.get("pre_job_sources") != expected_pre_job_sources
        or isolated_replay.get("approval") != expected_isolation_approval
        or isolated_replay.get("homotopy_endpoint")
        != expected_isolation_homotopy_endpoint
        or isolated_replay.get("homotopy_audit_source")
        != expected_isolation_homotopy_source
        or isolated_replay.get("schedule_direction_continuation_endpoint")
        != expected_isolation_continuation_endpoint
    ):
        raise ValueError("coarse repair audit worker isolation evidence is incomplete")

    started = _parse_canonical_utc_z(
        isolation.get("started_at_utc"), "repair audit worker start"
    )
    ended = _parse_canonical_utc_z(
        isolation.get("ended_at_utc"), "repair audit worker end"
    )
    if ended < started:
        raise ValueError("coarse repair audit worker timestamps are not ordered UTC")

    if isinstance(calibration_limit, bool) or not isinstance(calibration_limit, int):
        raise ValueError("coarse repair audit worker memory limit is invalid")
    memory_evidence = (
        _validate_coarse_repair_system_memory(memory["system_before_limit"]),
        _validate_coarse_repair_process_memory(
            memory["process_before_limit"], limit_bytes=calibration_limit
        ),
        _validate_coarse_repair_hard_limit(
            memory["hard_limit"], limit_bytes=calibration_limit
        ),
        _validate_coarse_repair_system_memory(memory["system_after_limit"]),
        _validate_coarse_repair_process_memory(
            memory["process_after_limit"], limit_bytes=calibration_limit
        ),
        _validate_coarse_repair_process_memory(
            memory["process_after_audit"], limit_bytes=calibration_limit
        ),
        _validate_coarse_repair_system_memory(memory["system_after_audit"]),
    )
    memory_process_ids = [record[0] for record in memory_evidence]
    memory_timestamps = [record[1] for record in memory_evidence]
    if (
        len(set(memory_process_ids)) != 1
        or any(
            later < earlier
            for earlier, later in zip(memory_timestamps, memory_timestamps[1:])
        )
        or memory_timestamps[0] < started
        or memory_timestamps[-1] > ended
    ):
        raise ValueError("coarse repair audit worker memory evidence PID/time differs")

    if (
        not evidence_path.is_file()
        or evidence_path.is_symlink()
        or _coarse_repair_path_has_link_component(evidence_path)
        or _sha256_file(expected_schedule_path)
        != local_schedule_binding.get("source_plan_sha256")
    ):
        raise ValueError("coarse repair audit worker evidence/schedule is stale")
    try:
        persisted_isolation = json.loads(
            evidence_path.read_text(encoding="utf-8"),
            object_pairs_hook=_coarse_repair_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"worker evidence contains {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("cannot parse coarse repair audit worker evidence") from error
    if persisted_isolation != isolation:
        raise ValueError("embedded and persisted worker isolation evidence differ")

    if list(output.glob("*.msh")) or list(output.glob("*.su2")):
        raise ValueError("coarse repair audit wrote a mesh")
    return dict(manifest)


def _handle_pipeline_coarse_repair_audit(args: argparse.Namespace) -> int:
    """Run one full in-memory repair discovery behind calibration RAM gates."""

    characteristic = float(args.characteristic_length)
    if not math.isfinite(characteristic) or characteristic <= 0.0:
        raise ValueError("coarse repair audit length must be finite and positive")
    timeout = float(args.timeout)
    if not math.isfinite(timeout) or not 0.0 < timeout <= 840.0:
        raise ValueError("coarse repair audit --timeout must be in (0, 840]")
    schedule_feasibility_endpoint = getattr(
        args, "schedule_feasibility_endpoint", None
    )
    if schedule_feasibility_endpoint not in (
        None,
        "minimum-growth-existing-direction-repair",
        "minimum-growth-owner-free-component-direction-audit",
        "minimum-growth-owner-free-component-pattern-audit",
        PHYSICAL_HOMOTOPY_REQUEST,
        SCHEDULE_CONTINUATION_REQUEST,
    ):
        raise ValueError("unsupported coarse repair schedule endpoint")
    replay_values = (
        getattr(args, "direction_audit_primary", None),
        getattr(args, "direction_audit_primary_sha256", None),
        getattr(args, "direction_audit_confirmation", None),
        getattr(args, "direction_audit_confirmation_sha256", None),
    )
    replay_requested = any(value is not None for value in replay_values)
    homotopy_requested = (
        schedule_feasibility_endpoint == PHYSICAL_HOMOTOPY_REQUEST
    )
    continuation_requested = (
        schedule_feasibility_endpoint == SCHEDULE_CONTINUATION_REQUEST
    )
    combined_replay_requested = homotopy_requested or continuation_requested
    if replay_requested and (
        any(value is None for value in replay_values)
        or (
            schedule_feasibility_endpoint is not None
            and not combined_replay_requested
        )
    ):
        raise ValueError(
            "physical-schedule fixed-direction replay requires both explicit "
            "audit manifests/SHA values and no search endpoint"
        )
    if combined_replay_requested and not replay_requested:
        raise ValueError(
            "physical-schedule combined audit requires both direction audit attestations"
        )
    continuation_values = (
        getattr(args, "homotopy_audit_manifest", None),
        getattr(args, "homotopy_audit_manifest_sha256", None),
    )
    if (
        continuation_requested
        and any(value is None for value in continuation_values)
        or not continuation_requested
        and any(value is not None for value in continuation_values)
    ):
        raise ValueError(
            "first-frontier direction continuation requires one explicit "
            "homotopy audit manifest/SHA pair"
        )

    raw_config = args.config.expanduser()
    if ".." in raw_config.parts:
        raise ValueError("coarse repair audit --config must not contain '..'")
    config_path = Path(os.path.abspath(raw_config)).resolve(strict=True)
    expected_config = Path(os.path.abspath(DEFAULT_COARSE_MESH_CONFIG)).resolve(
        strict=True
    )
    if (
        config_path != expected_config
        or not config_path.is_file()
        or raw_config.is_symlink()
    ):
        raise ValueError(
            "coarse repair audit requires the direct config/coarse_mesh.toml"
        )

    raw_output = args.output.expanduser()
    if ".." in raw_output.parts:
        raise ValueError("coarse repair audit --output must not contain '..'")
    lexical_output = Path(os.path.abspath(raw_output))
    lexical_root = Path(os.path.abspath(DEFAULT_COARSE_MESH_OUTPUT_ROOT))
    resolved_output = lexical_output.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=True)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if (
        resolved_output == resolved_root
        or resolved_output.parent != resolved_root
        or repository_root not in resolved_output.parents
        or lexical_output.exists()
        or resolved_output.exists()
        or lexical_output.is_symlink()
        or resolved_output.name.casefold() == "_worker_evidence"
    ):
        raise ValueError(
            "coarse repair audit output must be a new direct child of "
            f"{lexical_root}"
        )
    evidence_directory = resolved_root / "_worker_evidence" / resolved_output.name
    if evidence_directory.exists() or evidence_directory.is_symlink():
        raise ValueError("coarse repair audit evidence directory must be new")
    evidence_directory.mkdir(parents=True, exist_ok=False)

    try:
        config_document, config_sha256 = _read_cfdpipe_config(
            config_path, "coarse mesh config"
        )
        contract = normalize_coarse_mesh_config(
            config_document, repository_root=repository_root
        )
    except (CoarseMeshError, OSError, ValueError) as error:
        failure = _coarse_repair_audit_failure(
            stage="CONTRACT",
            code="CONTRACT_INVALID",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic,
            controller_argv=list(args.controller_argv),
        )
        report = _write_coarse_repair_audit_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse repair audit contract preflight failed; report: {report}"
        ) from error

    configured = contract.get("calibration_characteristic_lengths_m")
    first_characteristic = (
        float(configured[0]) if isinstance(configured, list) and configured else None
    )
    if first_characteristic is None or not math.isclose(
        characteristic,
        first_characteristic,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    ):
        failure = _coarse_repair_audit_failure(
            stage="POINT_SELECTION",
            code="FIRST_POINT_REQUIRED",
            message="repair discovery is restricted to the first projected point",
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic,
            controller_argv=list(args.controller_argv),
        )
        report = _write_coarse_repair_audit_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse repair audit point preflight failed; report: {report}"
        )

    try:
        projection_evidence = load_and_validate_projection_evidence(
            args.projection_manifest,
            args.projection_sha256,
            output_root=resolved_root,
            expected_config_path=config_path,
            expected_config_sha256=config_sha256,
            expected_coarse_contract_sha256=str(
                contract["normalized_config_sha256"]
            ),
            expected_characteristic_length_m=first_characteristic,
            expected_process_memory_limit_bytes=int(
                contract["resource"]["projection_worker_memory_limit_bytes"]
            ),
        )
        local_schedule_path, local_schedule_sha256, local_schedule_binding = (
            _load_coarse_local_schedule_binding(
                args.local_schedule,
                args.local_schedule_sha256,
                output_root=resolved_root,
                contract=contract,
                expected_coarse_contract_sha256=str(
                    projection_evidence["coarse_contract_sha256"]
                ),
            )
        )
    except (OSError, CoarseProjectionEvidenceError, ValueError) as error:
        failure = _coarse_repair_audit_failure(
            stage="BOUND_EVIDENCE",
            code="BOUND_EVIDENCE_INVALID",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic,
            controller_argv=list(args.controller_argv),
        )
        failure["inputs"].update(
            {
                "projection_manifest_path": str(args.projection_manifest),
                "projection_manifest_sha256": str(
                    args.projection_sha256
                ).casefold(),
                "local_schedule_path": str(args.local_schedule),
                "local_schedule_sha256": str(
                    args.local_schedule_sha256
                ).casefold(),
            }
        )
        report = _write_coarse_repair_audit_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse repair audit evidence preflight failed; report: {report}"
        ) from error

    direction_replay_approval = None
    physical_schedule_homotopy_endpoint = None
    schedule_direction_continuation_endpoint = None
    homotopy_audit_evidence = None
    homotopy_audit_manifest = None
    direction_audit_primary = None
    direction_audit_confirmation = None
    if replay_requested:
        try:
            direction_audit_primary = Path(
                os.path.abspath(args.direction_audit_primary.expanduser())
            ).resolve(strict=True)
            direction_audit_confirmation = Path(
                os.path.abspath(args.direction_audit_confirmation.expanduser())
            ).resolve(strict=True)
            if (
                direction_audit_primary == direction_audit_confirmation
                or direction_audit_primary.parent.parent != resolved_root
                or direction_audit_confirmation.parent.parent != resolved_root
                or _coarse_repair_path_has_link_component(
                    direction_audit_primary
                )
                or _coarse_repair_path_has_link_component(
                    direction_audit_confirmation
                )
            ):
                raise ValueError(
                    "direction audit sources must be distinct direct run artifacts"
                )
            expected_audit_strategy = (
                make_coarse_repair_audit_strategy_config(
                    contract,
                    characteristic,
                    local_schedule_binding=local_schedule_binding,
                    schedule_feasibility_endpoint=(
                        "minimum-growth-owner-free-component-pattern-audit"
                    ),
                )
            )
            quality = contract.get("quality")
            if not isinstance(quality, Mapping):
                raise ValueError("coarse quality contract is missing")
            direction_replay_approval = (
                load_direction_replay_consensus_approval(
                    direction_audit_primary,
                    str(args.direction_audit_primary_sha256).casefold(),
                    direction_audit_confirmation,
                    str(args.direction_audit_confirmation_sha256).casefold(),
                    expected_audit_strategy=expected_audit_strategy,
                    expected_coarse_contract_sha256=str(
                        projection_evidence["coarse_contract_sha256"]
                    ),
                    expected_characteristic_length_m=characteristic,
                    expected_local_schedule_binding=local_schedule_binding,
                    expected_projection_evidence=projection_evidence,
                    expected_projected_3d_elements=int(
                        projection_evidence["projected_3d_elements"]
                    ),
                    expected_worker_memory_limit_bytes=int(
                        contract["resource"][
                            "calibration_worker_memory_limit_bytes"
                        ]
                    ),
                    expected_minimum_prism_scaled_jacobian=float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    expected_minimum_core_tetra_gamma=float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                )
            )
            if combined_replay_requested:
                design = contract["boundary_layer_design"]
                physical_schedule_homotopy_endpoint = (
                    make_physical_schedule_homotopy_endpoint(
                        baseline_binding_sha256=str(
                            direction_replay_approval[
                                "local_schedule_binding_sha256"
                            ]
                        ),
                        first_layer_height_m=float(
                            design["first_layer_height_m"]
                        ),
                        layer_count=int(design["layer_count"]),
                    )
                )
                physical_schedule_homotopy_endpoint = (
                    validate_physical_schedule_homotopy_endpoint(
                        physical_schedule_homotopy_endpoint,
                        expected_binding_sha256=str(
                            direction_replay_approval[
                                "local_schedule_binding_sha256"
                            ]
                        ),
                        expected_first_layer_height_m=float(
                            design["first_layer_height_m"]
                        ),
                        expected_layer_count=int(design["layer_count"]),
                    )
                )
            if continuation_requested:
                homotopy_audit_manifest = Path(
                    os.path.abspath(
                        args.homotopy_audit_manifest.expanduser()
                    )
                ).resolve(strict=True)
                if (
                    homotopy_audit_manifest.parent.parent != resolved_root
                    or homotopy_audit_manifest
                    in (direction_audit_primary, direction_audit_confirmation)
                    or _coarse_repair_path_has_link_component(
                        homotopy_audit_manifest
                    )
                ):
                    raise ValueError(
                        "homotopy audit must be one independent direct run artifact"
                    )
                source_quality = direction_replay_approval[
                    "source_minimum_growth_final_quality"
                ]
                expected_layer_count = int(
                    contract["boundary_layer_design"]["layer_count"]
                )
                expected_surface_count = len(
                    contract["wall_surface_fingerprints"]
                )
                expected_surface_schedules = (
                    _physical_surface_schedule_values(
                        local_schedule_binding,
                        expected_surface_count=expected_surface_count,
                        expected_layer_count=expected_layer_count,
                    )
                )
                homotopy_audit_evidence = (
                    load_and_validate_physical_homotopy_audit_manifest(
                        homotopy_audit_manifest,
                        str(
                            args.homotopy_audit_manifest_sha256
                        ).casefold(),
                        output_root=resolved_root,
                        expected_contract_sha256=str(
                            projection_evidence["coarse_contract_sha256"]
                        ),
                        expected_characteristic_length_m=characteristic,
                        expected_projection_evidence=projection_evidence,
                        expected_local_schedule_binding=(
                            local_schedule_binding
                        ),
                        expected_approval=direction_replay_approval,
                        expected_endpoint=(
                            physical_schedule_homotopy_endpoint
                        ),
                        expected_projected_3d_elements=int(
                            projection_evidence["projected_3d_elements"]
                        ),
                        expected_prism_element_count=int(
                            source_quality["prism_element_count"]
                        ),
                        expected_core_element_count=int(
                            source_quality["core_element_count"]
                        ),
                        expected_layer_count=expected_layer_count,
                        expected_surface_count=expected_surface_count,
                        expected_surface_schedules=(
                            expected_surface_schedules
                        ),
                        expected_minimum_prism_scaled_jacobian=float(
                            quality["minimum_prism_scaled_jacobian"]
                        ),
                        expected_minimum_core_tetra_gamma=float(
                            quality["minimum_core_tetra_gamma"]
                        ),
                    )
                )
                endpoint_arguments = {
                    "homotopy_manifest_sha256": (
                        homotopy_audit_evidence["sha256"]
                    ),
                    "homotopy_discovery": homotopy_audit_evidence[
                        "discovery"
                    ],
                    "homotopy_endpoint_sha256": (
                        homotopy_audit_evidence["discovery"].get(
                            "source_bindings", {}
                        ).get(
                            "homotopy_endpoint_sha256",
                            physical_schedule_homotopy_endpoint[
                                "endpoint_sha256"
                            ],
                        )
                    ),
                    "direction_replay_approval_sha256": (
                        homotopy_audit_evidence["discovery"].get(
                            "source_bindings", {}
                        ).get(
                            "direction_replay_approval_sha256",
                            direction_replay_approval["approval_sha256"],
                        )
                    ),
                    "local_schedule_binding_sha256": (
                        homotopy_audit_evidence["discovery"].get(
                            "source_bindings", {}
                        ).get(
                            "local_schedule_binding_sha256",
                            local_schedule_binding["binding_sha256"],
                        )
                    ),
                    "minimum_prism_scaled_jacobian_for_pass": float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    "minimum_core_tetra_gamma_for_pass": float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                    "layer_count": expected_layer_count,
                    "first_layer_height_m": float(
                        contract["boundary_layer_design"][
                            "first_layer_height_m"
                        ]
                    ),
                }
                schedule_direction_continuation_endpoint = (
                    make_schedule_frontier_direction_endpoint(
                        **endpoint_arguments
                    )
                )
                schedule_direction_continuation_endpoint = (
                    validate_schedule_frontier_direction_endpoint(
                        schedule_direction_continuation_endpoint,
                        **endpoint_arguments,
                    )
                )
        except (
            OSError,
            CoarseDirectionReplayError,
            CoarseScheduleContinuationError,
            CoarseRepairAuditError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            failure = _coarse_repair_audit_failure(
                stage="DIRECTION_REPLAY_CONSENSUS",
                code="DIRECTION_REPLAY_CONSENSUS_INVALID",
                message=str(error),
                config_path=config_path,
                output_directory=lexical_output,
                characteristic_length_m=characteristic,
                controller_argv=list(args.controller_argv),
            )
            report = _write_coarse_repair_audit_preflight(
                evidence_directory, failure
            )
            raise ValueError(
                f"direction replay consensus preflight failed; report: {report}"
            ) from error

    try:
        system_snapshot = capture_system_snapshot(evidence_directory)
        preflight = evaluate_first_point_preflight(
            contract, system_snapshot, timeout_seconds=timeout
        )
    except (OSError, CoarseCalibrationGateError, KeyError, ValueError) as error:
        failure = _coarse_repair_audit_failure(
            stage="RESOURCE_SNAPSHOT",
            code="RESOURCE_SNAPSHOT_FAILED",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic,
            controller_argv=list(args.controller_argv),
        )
        report = _write_coarse_repair_audit_preflight(
            evidence_directory, failure
        )
        raise ValueError(
            f"coarse repair audit resource snapshot failed; report: {report}"
        ) from error

    preflight["schema"] = "cfdpipe.coarse_repair_audit_preflight.v1"
    preflight["audit_only"] = True
    preflight["calibration_PASS_authorized"] = False
    preflight["production_mesh_eligible"] = False
    preflight["mesh_written"] = False
    preflight["policy"]["command_runner_constructed"] = False
    preflight["inputs"].update(
        {
            "config_path": str(config_path),
            "config_sha256": config_sha256,
            "coarse_contract_sha256": contract["normalized_config_sha256"],
            "output_directory": str(lexical_output),
            "selected_characteristic_length_m": characteristic,
            "projection_manifest_path": projection_evidence["path"],
            "projection_manifest_sha256": projection_evidence["sha256"],
            "projection_evidence_sha256": projection_evidence[
                "evidence_sha256"
            ],
            "local_schedule_path": str(local_schedule_path),
            "local_schedule_sha256": local_schedule_sha256,
            "local_schedule_binding_sha256": local_schedule_binding[
                "binding_sha256"
            ],
            "schedule_feasibility_endpoint": schedule_feasibility_endpoint,
            "controller_argv": list(args.controller_argv),
        }
    )
    if direction_replay_approval is not None:
        preflight["inputs"].update(
            {
                "direction_audit_primary_path": str(direction_audit_primary),
                "direction_audit_primary_sha256": str(
                    args.direction_audit_primary_sha256
                ).casefold(),
                "direction_audit_confirmation_path": str(
                    direction_audit_confirmation
                ),
                "direction_audit_confirmation_sha256": str(
                    args.direction_audit_confirmation_sha256
                ).casefold(),
                "direction_replay_approval_sha256": (
                    direction_replay_approval["approval_sha256"]
                ),
                "audit_only_direction_replay_authorized": True,
                "mesh_write_authorized": False,
            }
        )
        if combined_replay_requested:
            preflight["inputs"][
                "physical_schedule_homotopy_endpoint_sha256"
            ] = physical_schedule_homotopy_endpoint["endpoint_sha256"]
        if continuation_requested:
            preflight["inputs"].update(
                {
                    "homotopy_audit_manifest_path": str(
                        homotopy_audit_manifest
                    ),
                    "homotopy_audit_manifest_sha256": (
                        homotopy_audit_evidence["sha256"]
                    ),
                    "schedule_direction_continuation_endpoint_sha256": (
                        schedule_direction_continuation_endpoint[
                            "endpoint_sha256"
                        ]
                    ),
                }
            )
    if preflight.get("status") != "PASS":
        report = _write_coarse_repair_audit_preflight(
            evidence_directory, preflight
        )
        raise ValueError(
            f"coarse repair audit resource preflight failed; report: {report}"
        )

    python_executable = Path(sys.executable).expanduser().resolve(strict=True)
    source_root = (repository_root / "src").resolve(strict=True)
    runner = CommandRunner(evidence_directory, live_output=not args.quiet)
    preflight["policy"]["command_runner_constructed"] = True
    preflight_path = _write_coarse_repair_audit_preflight(
        evidence_directory, preflight
    )
    worker_arguments_list = [
        "-m",
        "cfdpipe.coarse_repair_audit_worker",
        "--config",
        str(config_path),
        "--config-sha256",
        config_sha256,
        "--coarse-contract-sha256",
        str(contract["normalized_config_sha256"]),
        "--characteristic-length",
        format(characteristic, ".17g"),
        "--projection-manifest",
        str(Path(projection_evidence["path"])),
        "--projection-sha256",
        str(projection_evidence["sha256"]),
        "--local-schedule",
        str(local_schedule_path),
        "--local-schedule-sha256",
        local_schedule_sha256,
    ]
    if schedule_feasibility_endpoint is not None:
        worker_arguments_list.extend(
            [
                "--schedule-feasibility-endpoint",
                schedule_feasibility_endpoint,
            ]
        )
    if direction_replay_approval is not None:
        worker_arguments_list.extend(
            [
                "--direction-audit-primary",
                str(direction_audit_primary),
                "--direction-audit-primary-sha256",
                str(args.direction_audit_primary_sha256).casefold(),
                "--direction-audit-confirmation",
                str(direction_audit_confirmation),
                "--direction-audit-confirmation-sha256",
                str(args.direction_audit_confirmation_sha256).casefold(),
                "--direction-replay-approval-sha256",
                str(direction_replay_approval["approval_sha256"]),
            ]
        )
        if combined_replay_requested:
            worker_arguments_list.extend(
                [
                    "--physical-schedule-homotopy-endpoint-sha256",
                    str(
                        physical_schedule_homotopy_endpoint[
                            "endpoint_sha256"
                        ]
                    ),
                ]
            )
        if continuation_requested:
            worker_arguments_list.extend(
                [
                    "--homotopy-audit-manifest",
                    str(homotopy_audit_manifest),
                    "--homotopy-audit-manifest-sha256",
                    str(homotopy_audit_evidence["sha256"]),
                    "--schedule-direction-continuation-endpoint-sha256",
                    str(
                        schedule_direction_continuation_endpoint[
                            "endpoint_sha256"
                        ]
                    ),
                ]
            )
    worker_arguments_list.extend(["--output", str(lexical_output)])
    worker_arguments = tuple(worker_arguments_list)
    abort_check = _make_physical_memory_abort_check(contract)
    try:
        result = runner.run(
            python_executable,
            worker_arguments,
            cwd=repository_root,
            env={
                "PYTHONPATH": str(source_root),
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            },
            timeout=timeout,
            check=False,
            live_output=not args.quiet,
            name="coarse-repair-audit-worker",
            stdout_log="worker.stdout.log",
            stderr_log="worker.stderr.log",
            metadata_log="worker.command.json",
            abort_check=abort_check,
            abort_check_interval_seconds=0.5,
        )
    except CommandExecutionError as error:
        if error.result.resource_aborted:
            abort_manifest_path = evidence_directory / RESOURCE_ABORT_MANIFEST_NAME
            try:
                publish_coarse_repair_resource_abort_manifest(
                    evidence_directory=evidence_directory,
                    command_metadata_path=(
                        evidence_directory / "worker.command.json"
                    ),
                    source_brep_path=Path(str(contract["pipeline_brep_path"])),
                    expected_source_sha256=str(
                        contract["provenance"]["pipeline_brep_sha256"]
                    ),
                    output_directory=lexical_output,
                )
            except (
                CoarseRepairAuditError,
                KeyError,
                OSError,
                TypeError,
                ValueError,
            ) as publication_error:
                raise CommandExecutionError(
                    error.result,
                    "coarse repair audit resource-abort evidence publication "
                    f"failed: {publication_error}; original command evidence "
                    f"remains in {evidence_directory}",
                ) from error
            raise CommandExecutionError(
                error.result,
                "coarse repair audit worker was stopped by the physical-memory "
                f"guard; strict FAIL evidence: {abort_manifest_path}",
            ) from error
        raise CommandExecutionError(
            error.result,
            "coarse repair audit worker failed; evidence remains in "
            f"{evidence_directory}",
        ) from error

    manifest_path = lexical_output / "coarse_repair_audit_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CommandExecutionError(
            result,
            "coarse repair audit worker produced no safe manifest; evidence remains "
            f"in {evidence_directory}",
        )

    def reject_json_constant(value: str) -> None:
        raise ValueError(f"coarse repair audit manifest contains {value}")

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_coarse_repair_strict_json_object,
            parse_constant=reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"cannot read coarse repair audit manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("coarse repair audit manifest must be a JSON object")
    structured_physical_incomplete = (
        direction_replay_approval is not None
        and result.returncode == 2
        and manifest.get("status") == "INCOMPLETE"
    )
    if result.returncode != 0 and not structured_physical_incomplete:
        raise CommandExecutionError(
            result,
            "coarse repair audit worker failed; evidence remains in "
            f"{evidence_directory}",
        )
    validated = _validate_coarse_repair_audit_pass_manifest(
        manifest,
        manifest_path=manifest_path,
        expected_output_directory=resolved_output,
        expected_config_path=config_path,
        expected_config_sha256=config_sha256,
        contract=contract,
        expected_characteristic_length_m=characteristic,
        projection_evidence=projection_evidence,
        local_schedule_binding=local_schedule_binding,
        expected_schedule_feasibility_endpoint=(
            schedule_feasibility_endpoint
        ),
        expected_direction_replay_approval=direction_replay_approval,
        expected_direction_replay_inputs=(
            {
                "primary_path": direction_audit_primary,
                "primary_sha256": str(
                    args.direction_audit_primary_sha256
                ).casefold(),
                "confirmation_path": direction_audit_confirmation,
                "confirmation_sha256": str(
                    args.direction_audit_confirmation_sha256
                ).casefold(),
            }
            if direction_replay_approval is not None
            else None
        ),
        expected_physical_schedule_homotopy_endpoint=(
            physical_schedule_homotopy_endpoint
            if combined_replay_requested
            else None
        ),
        expected_schedule_direction_continuation_endpoint=(
            schedule_direction_continuation_endpoint
            if continuation_requested
            else None
        ),
        expected_homotopy_audit_evidence=(
            homotopy_audit_evidence if continuation_requested else None
        ),
        allow_physical_incomplete=structured_physical_incomplete,
    )
    discovery_identity = (
        {
            "discovery_sha256": validated["repair_discovery"][
                "discovery_sha256"
            ]
        }
        if direction_replay_approval is not None
        else (
        {
            "stable_observation_sha256": validated["repair_discovery"][
                "stable_observation_sha256"
            ]
        }
        if schedule_feasibility_endpoint
        in (
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
        )
        else {
            "stable_signature_sha256": validated["repair_discovery"][
                "stable_signature_sha256"
            ]
        }
        )
    )
    _print_json(
        {
            "status": validated["status"],
            "audit_only": True,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "manifest": str(manifest_path.resolve()),
            "manifest_sha256": _sha256_file(manifest_path),
            **discovery_identity,
            "observed_counts": validated["repair_discovery"][
                "observed_counts"
            ],
            "preflight": str(preflight_path.resolve()),
            "worker_command": result.as_metadata(),
        }
    )
    return 2 if structured_physical_incomplete else 0


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_resolved_path(value: object, expected: Path) -> bool:
    try:
        actual = Path(str(value)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def _validate_coarse_calibration_pass_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    expected_output_directory: Path,
    expected_config_sha256: str,
    contract: Mapping[str, Any],
    expected_characteristic_length_m: float,
    projection_evidence: Mapping[str, Any],
    local_schedule_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate every safety-critical calibration claim after worker exit."""

    if not isinstance(manifest, Mapping):
        raise ValueError("coarse calibration manifest must be a JSON object")
    output = expected_output_directory.resolve(strict=True)
    resolved_manifest = manifest_path.resolve(strict=True)
    if resolved_manifest.parent != output:
        raise ValueError("coarse calibration manifest is outside its run directory")
    contract_sha256 = str(contract.get("normalized_config_sha256", ""))
    configured_lengths = contract.get("calibration_characteristic_lengths_m")
    if (
        not _is_lower_sha256(expected_config_sha256)
        or not _is_lower_sha256(contract_sha256)
        or not isinstance(configured_lengths, list)
        or not configured_lengths
    ):
        raise ValueError("current calibration contract identity is incomplete")
    first_characteristic = float(configured_lengths[0])
    selected = manifest.get("characteristic_length_m")
    selected_valid = (
        isinstance(selected, (int, float))
        and not isinstance(selected, bool)
        and math.isfinite(float(selected))
        and math.isclose(
            float(selected),
            expected_characteristic_length_m,
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
    )
    if (
        manifest.get("schema")
        != "cfdpipe.coarse_mesh_calibration_manifest.v1"
        or manifest.get("status") != "PASS"
        or manifest.get("calibration_only") is not True
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
        or manifest.get("external_commands") != []
        or manifest.get("external_stderr") is not None
        or manifest.get("error") is not None
        or manifest.get("source_unchanged") is not True
        or manifest.get("coarse_contract_sha256") != contract_sha256
        or not selected_valid
        or not _same_resolved_path(manifest.get("output_directory"), output)
    ):
        raise ValueError("coarse calibration manifest header is not a strict PASS")

    expected_projection_count = projection_evidence.get("projected_3d_elements")
    if (
        isinstance(expected_projection_count, bool)
        or not isinstance(expected_projection_count, int)
        or expected_projection_count <= 0
    ):
        raise ValueError("controller projection evidence has no positive count")
    try:
        embedded_projection = validate_projection_evidence_record(
            manifest.get("projection_evidence"),
            expected_path=str(projection_evidence.get("path", "")),
            expected_sha256=str(projection_evidence.get("sha256", "")),
            expected_config_sha256=expected_config_sha256,
            expected_coarse_contract_sha256=contract_sha256,
            expected_characteristic_length_m=first_characteristic,
            expected_projected_3d_elements=expected_projection_count,
        )
    except CoarseProjectionEvidenceError as error:
        raise ValueError(
            f"calibration projection lineage is invalid: {error}"
        ) from error
    if embedded_projection.get("evidence_sha256") != projection_evidence.get(
        "evidence_sha256"
    ):
        raise ValueError("calibration projection lineage changed in the worker")

    sessions = manifest.get("gmsh_sessions")
    if not isinstance(sessions, Mapping) or any(
        not isinstance(sessions.get(phase), Mapping)
        or sessions[phase].get("finalize_called") is not True
        or sessions[phase].get("cleanup_errors") != []
        for phase in ("build", "readback")
    ):
        raise ValueError("calibration Gmsh lifecycle evidence is incomplete")
    generation = manifest.get("generation_audit")
    readback = manifest.get("readback_audit")
    generated_count = (
        generation.get("element_count_3d")
        if isinstance(generation, Mapping)
        else None
    )
    readback_count = (
        readback.get("element_count_3d")
        if isinstance(readback, Mapping)
        else None
    )
    calibration_cap = contract.get("calibration_maximum_3d_elements")
    if (
        not isinstance(generation, Mapping)
        or generation.get("status") != "PASS"
        or not isinstance(readback, Mapping)
        or readback.get("status") != "PASS"
        or isinstance(generated_count, bool)
        or not isinstance(generated_count, int)
        or generated_count <= 0
        or generated_count != readback_count
        or isinstance(calibration_cap, bool)
        or not isinstance(calibration_cap, int)
        or generated_count > calibration_cap
        or generation.get("element_types_3d")
        != readback.get("element_types_3d")
        or generation.get("marker_face_counts")
        != readback.get("marker_face_counts")
    ):
        raise ValueError("calibration generation/readback evidence is inconsistent")

    first_point = math.isclose(
        expected_characteristic_length_m,
        first_characteristic,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    )
    crosscheck = manifest.get("projection_generation_count_crosscheck")
    if not isinstance(crosscheck, Mapping):
        raise ValueError("calibration projection count crosscheck is missing")
    if first_point:
        crosscheck_valid = (
            crosscheck.get("status") == "PASS"
            and crosscheck.get("required_for_first_configured_point") is True
            and crosscheck.get("projected_3d_elements")
            == expected_projection_count
            and crosscheck.get("generated_3d_elements") == generated_count
            and crosscheck.get("exact_match") is True
            and generated_count == expected_projection_count
        )
    else:
        crosscheck_valid = (
            crosscheck.get("status") == "NOT_APPLICABLE"
            and crosscheck.get("required_for_first_configured_point") is False
            and crosscheck.get("projected_3d_elements")
            == expected_projection_count
            and crosscheck.get("generated_3d_elements") == generated_count
            and crosscheck.get("exact_match") is None
            and crosscheck.get("second_point_uses_first_projection_lineage_only")
            is True
        )
    if not crosscheck_valid:
        raise ValueError("calibration projection count crosscheck is invalid")

    strategy = manifest.get("strategy_config")
    strategy_hash = manifest.get("strategy_config_sha256")
    strategy_schedule = (
        strategy.get("local_boundary_layer_schedule")
        if isinstance(strategy, Mapping)
        else None
    )
    if (
        not isinstance(strategy, Mapping)
        or strategy.get("coarse_contract_sha256") != contract_sha256
        or not _is_lower_sha256(strategy_hash)
        or strategy.get("normalized_config_sha256") != strategy_hash
        or not isinstance(strategy_schedule, Mapping)
        or strategy_schedule.get("binding_sha256")
        != local_schedule_binding.get("binding_sha256")
        or strategy_schedule.get("source_plan_path")
        != local_schedule_binding.get("source_plan_path")
        or strategy_schedule.get("source_plan_sha256")
        != local_schedule_binding.get("source_plan_sha256")
    ):
        raise ValueError("calibration strategy/schedule lineage is incomplete")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {"mesh.msh", "mesh.su2"}:
        raise ValueError("calibration outputs are incomplete")
    for name in ("mesh.msh", "mesh.su2"):
        record = outputs.get(name)
        expected_path = output / name
        if not isinstance(record, Mapping) or not expected_path.is_file():
            raise ValueError(f"calibration output {name} is missing")
        actual_size = expected_path.stat().st_size
        actual_sha256 = _sha256_file(expected_path)
        if (
            not _same_resolved_path(record.get("path"), expected_path)
            or record.get("size_bytes") != actual_size
            or actual_size <= 0
            or record.get("sha256") != actual_sha256
        ):
            raise ValueError(f"calibration output {name} evidence is stale")
    su2 = manifest.get("su2_validation")
    if (
        not isinstance(su2, Mapping)
        or su2.get("status") != "PASS"
        or su2.get("nelem") != generated_count
        or su2.get("sha256") != outputs["mesh.su2"].get("sha256")
        or su2.get("file_size_bytes") != outputs["mesh.su2"].get("size_bytes")
    ):
        raise ValueError("calibration SU2 text audit is incomplete")
    deferred = manifest.get("deferred_diagnostic_finalization")
    eligibility = manifest.get("resource_sample_eligibility")
    if (
        not isinstance(deferred, Mapping)
        or deferred.get("status") != "PASS"
        or manifest.get("diagnostic_quality_status") != "PASS"
        or manifest.get("requires_quality_improvement_before_production") is not False
        or manifest.get("resource_sample_eligible") is not True
        or not isinstance(eligibility, Mapping)
        or eligibility.get("quality_gate_pass") is not True
        or eligibility.get("elapsed_within_limit") is not True
        or eligibility.get("peak_working_set_valid") is not True
    ):
        raise ValueError("calibration quality/resource eligibility is incomplete")

    isolation = manifest.get("worker_isolation")
    worker = isolation.get("worker") if isinstance(isolation, Mapping) else None
    memory = (
        isolation.get("worker_memory") if isinstance(isolation, Mapping) else None
    )
    hard_limit = memory.get("hard_limit") if isinstance(memory, Mapping) else None
    job = hard_limit.get("job_object") if isinstance(hard_limit, Mapping) else None
    schedule = (
        isolation.get("local_schedule") if isinstance(isolation, Mapping) else None
    )
    binding = schedule.get("binding") if isinstance(schedule, Mapping) else None
    calibration_limit = contract.get("resource", {}).get(
        "calibration_worker_memory_limit_bytes"
    )
    required_worker_flags = (
        "hard_limit_installed_before_heavy_import",
        "heavy_dependencies_imported",
        "normalized_resource_matches_raw",
        "local_schedule_bound_after_heavy_import",
        "builder_called",
    )
    if (
        not isinstance(isolation, Mapping)
        or isolation.get("schema")
        != "cfdpipe.coarse_mesh_calibration_worker.v1"
        or isolation.get("status") != "PASS"
        or isolation.get("config_sha256") != expected_config_sha256
        or isolation.get("coarse_contract_sha256") != contract_sha256
        or isolation.get("worker_error") is not None
        or not isinstance(worker, Mapping)
        or any(worker.get(flag) is not True for flag in required_worker_flags)
        or not _same_resolved_path(worker.get("output_directory"), output)
        or not isinstance(memory, Mapping)
        or not isinstance(hard_limit, Mapping)
        or hard_limit.get("status") != "PASS"
        or hard_limit.get("requested_process_memory_limit_bytes")
        != calibration_limit
        or not isinstance(job, Mapping)
        or job.get("process_memory_limit_bytes") != calibration_limit
        or job.get("process_assigned") is not True
        or job.get("handle_retained_for_process_lifetime") is not True
        or any(
            not isinstance(memory.get(name), Mapping)
            or memory[name].get("status") != "PASS"
            for name in (
                "system_before_limit",
                "process_before_limit",
                "system_after_limit",
                "process_after_limit",
                "system_after_calibration",
                "process_after_calibration",
            )
        )
        or not isinstance(binding, Mapping)
        or binding.get("status") != "PASS"
        or binding.get("binding_sha256")
        != local_schedule_binding.get("binding_sha256")
        or binding.get("source_plan_path")
        != local_schedule_binding.get("source_plan_path")
        or binding.get("source_plan_sha256")
        != local_schedule_binding.get("source_plan_sha256")
    ):
        raise ValueError("calibration worker isolation evidence is incomplete")
    try:
        isolated_projection = validate_projection_evidence_record(
            isolation.get("projection_evidence"),
            expected_path=str(projection_evidence.get("path", "")),
            expected_sha256=str(projection_evidence.get("sha256", "")),
            expected_config_sha256=expected_config_sha256,
            expected_coarse_contract_sha256=contract_sha256,
            expected_characteristic_length_m=first_characteristic,
            expected_projected_3d_elements=expected_projection_count,
        )
    except CoarseProjectionEvidenceError as error:
        raise ValueError(
            f"calibration worker projection lineage is invalid: {error}"
        ) from error
    if isolated_projection.get("evidence_sha256") != projection_evidence.get(
        "evidence_sha256"
    ):
        raise ValueError("calibration worker projection lineage changed")
    return dict(manifest)


def _load_first_coarse_calibration_manifest(
    path: Path,
    *,
    output_root: Path,
    expected_config_sha256: str,
    contract: Mapping[str, Any],
    expected_characteristic_length_m: float,
    projection_evidence: Mapping[str, Any],
    local_schedule_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    raw = path.expanduser()
    if ".." in raw.parts:
        raise ValueError("--first-manifest must not contain '..'")
    lexical = Path(os.path.abspath(raw))
    if lexical.is_symlink():
        raise ValueError("--first-manifest must not be a symbolic link")
    resolved = lexical.resolve(strict=True)
    resolved_root = output_root.resolve(strict=False)
    if (
        not resolved.is_file()
        or resolved.name != "coarse_calibration_manifest.json"
        or resolved_root not in resolved.parents
    ):
        raise ValueError(
            "--first-manifest must be an explicit calibration manifest below "
            f"{output_root}"
        )

    def reject_json_constant(value: str) -> None:
        raise ValueError(f"first calibration manifest contains {value}")

    try:
        raw_bytes = resolved.read_bytes()
        manifest = json.loads(
            raw_bytes.decode("utf-8"), parse_constant=reject_json_constant
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"cannot read first calibration manifest: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise ValueError("first calibration manifest must be a JSON object")
    validated = _validate_coarse_calibration_pass_manifest(
        manifest,
        manifest_path=resolved,
        expected_output_directory=resolved.parent,
        expected_config_sha256=expected_config_sha256,
        contract=contract,
        expected_characteristic_length_m=expected_characteristic_length_m,
        projection_evidence=projection_evidence,
        local_schedule_binding=local_schedule_binding,
    )
    return validated, {
        "path": str(resolved),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def _load_coarse_local_schedule_binding(
    path: Path,
    expected_sha256: str,
    *,
    output_root: Path,
    contract: dict[str, Any],
    expected_coarse_contract_sha256: str | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    """Resolve and fully bind one explicit local schedule before worker launch."""

    raw = path.expanduser()
    if ".." in raw.parts:
        raise ValueError("--local-schedule must not contain '..'")
    lexical = Path(os.path.abspath(raw))
    current = lexical
    while True:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (
            callable(is_junction) and is_junction()
        ):
            raise ValueError("--local-schedule path must not use links or junctions")
        if current.parent == current:
            break
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        allowed_root = output_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("--local-schedule does not exist") from error
    if (
        not resolved.is_file()
        or resolved.name != "boundary_layer_local_schedule.json"
        or allowed_root not in resolved.parents
        or "_worker_evidence" in {
            part.casefold() for part in resolved.relative_to(allowed_root).parts
        }
    ):
        raise ValueError(
            "--local-schedule must be an explicit boundary_layer_local_schedule.json "
            f"below {allowed_root} and outside _worker_evidence"
        )
    expected = str(expected_sha256).casefold()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("--local-schedule-sha256 must be 64 lowercase hex digits")
    try:
        binding = load_and_bind_boundary_layer_local_schedule(
            coarse_contract=contract,
            plan_path=resolved,
            plan_sha256=expected,
            expected_coarse_contract_sha256=expected_coarse_contract_sha256,
        )
    except BoundaryLayerScheduleBindingError as error:
        raise ValueError(f"local boundary-layer schedule is invalid: {error}") from error
    if (
        not isinstance(binding, dict)
        or binding.get("status") != "PASS"
        or binding.get("source_plan_path") != str(resolved)
        or binding.get("source_plan_sha256") != expected
    ):
        raise ValueError("local boundary-layer schedule binding is not a strict PASS")
    return resolved, expected, binding


def _handle_pipeline_coarse_mesh_calibrate(args: argparse.Namespace) -> int:
    """Launch one bounded Gmsh calibration in the isolated Python worker."""

    characteristic_length = float(args.characteristic_length)
    if not math.isfinite(characteristic_length) or characteristic_length <= 0.0:
        raise ValueError("--characteristic-length must be finite and positive")
    timeout = float(args.timeout)
    if not math.isfinite(timeout) or not 0.0 < timeout <= 840.0:
        raise ValueError("coarse calibration --timeout must be in (0, 840]")

    raw_config = args.config.expanduser()
    if ".." in raw_config.parts:
        raise ValueError("coarse calibration --config must not contain '..'")
    config_path = Path(os.path.abspath(raw_config)).resolve(strict=True)
    expected_config = Path(
        os.path.abspath(DEFAULT_COARSE_MESH_CONFIG)
    ).resolve(strict=True)
    if (
        os.path.normcase(str(config_path))
        != os.path.normcase(str(expected_config))
        or not config_path.is_file()
    ):
        raise ValueError(
            "coarse calibration requires the repository config/coarse_mesh.toml"
        )

    raw_output = args.output.expanduser()
    if ".." in raw_output.parts:
        raise ValueError("coarse calibration --output must not contain '..'")
    lexical_output = Path(os.path.abspath(raw_output))
    lexical_root = Path(os.path.abspath(DEFAULT_COARSE_MESH_OUTPUT_ROOT))
    resolved_output = lexical_output.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=False)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if resolved_root != repository_root and repository_root not in resolved_root.parents:
        raise ValueError("coarse calibration root resolves outside the repository")
    if resolved_output == resolved_root or resolved_root not in resolved_output.parents:
        raise ValueError(
            f"coarse calibration output must be a new child of {lexical_root}"
        )
    relative_output = resolved_output.relative_to(resolved_root)
    if relative_output.parts[0].casefold() == "_worker_evidence":
        raise ValueError("coarse calibration output uses the reserved evidence directory")
    if lexical_output.exists() or lexical_output.is_symlink() or resolved_output.exists():
        raise ValueError("coarse calibration output must not already exist")

    python_executable = Path(sys.executable).expanduser().resolve(strict=True)
    if not python_executable.is_file() or not python_executable.is_absolute():
        raise ValueError("the current Python executable is not an absolute file")
    source_root = (repository_root / "src").resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("repository src directory is unavailable to the worker")

    evidence_directory = (
        lexical_root / "_worker_evidence" / relative_output
    ).resolve(strict=False)
    if resolved_root not in evidence_directory.parents:
        raise ValueError("coarse calibration evidence resolves outside its root")
    if evidence_directory.exists() or evidence_directory.is_symlink():
        raise ValueError("coarse calibration evidence directory must be new")
    evidence_directory.mkdir(parents=True, exist_ok=False)

    try:
        config_document, config_sha256 = _read_cfdpipe_config(
            config_path, "coarse mesh config"
        )
        contract = normalize_coarse_mesh_config(
            config_document, repository_root=repository_root
        )
    except (CoarseMeshError, OSError, ValueError) as error:
        report = _coarse_calibration_failure_report(
            stage="CONTRACT",
            code="CONTRACT_INVALID",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(
            f"coarse calibration contract preflight failed; report: {report_path}"
        ) from error

    configured_sizes = [
        float(value)
        for value in contract["calibration_characteristic_lengths_m"]
    ]
    point_index = next(
        (
            index
            for index, configured in enumerate(configured_sizes)
            if math.isclose(
                characteristic_length,
                configured,
                rel_tol=1.0e-12,
                abs_tol=0.0,
            )
        ),
        None,
    )
    if point_index is None or point_index not in (0, 1):
        report = _coarse_calibration_failure_report(
            stage="POINT_SELECTION",
            code="CHARACTERISTIC_LENGTH_NOT_AUTHORIZED",
            message=(
                f"characteristic length {characteristic_length} is not one of "
                f"the first two contract points {configured_sizes[:2]}"
            ),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(
            f"coarse calibration point selection failed; report: {report_path}"
        )

    try:
        projection_evidence = load_and_validate_projection_evidence(
            args.projection_manifest,
            args.projection_sha256,
            output_root=lexical_root,
            expected_config_path=config_path,
            expected_config_sha256=config_sha256,
            expected_coarse_contract_sha256=str(
                contract["normalized_config_sha256"]
            ),
            expected_characteristic_length_m=configured_sizes[0],
            expected_process_memory_limit_bytes=int(
                contract["resource"]["projection_worker_memory_limit_bytes"]
            ),
        )
    except (OSError, CoarseProjectionEvidenceError, ValueError) as error:
        report = _coarse_calibration_failure_report(
            stage="PROJECTION_EVIDENCE",
            code="PROJECTION_EVIDENCE_INVALID",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report["inputs"].update(
            {
                "projection_manifest_path": str(args.projection_manifest),
                "projection_manifest_sha256": str(
                    args.projection_sha256
                ).casefold(),
            }
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(
            f"coarse calibration projection preflight failed; report: {report_path}"
        ) from error

    try:
        local_schedule_path, local_schedule_sha256, local_schedule_binding = (
            _load_coarse_local_schedule_binding(
                args.local_schedule,
                args.local_schedule_sha256,
                output_root=lexical_root,
                contract=contract,
            )
        )
    except (OSError, ValueError) as error:
        report = _coarse_calibration_failure_report(
            stage="LOCAL_SCHEDULE",
            code="LOCAL_SCHEDULE_INVALID",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report["inputs"].update(
            {
                "local_schedule_path": str(args.local_schedule),
                "local_schedule_sha256": str(
                    args.local_schedule_sha256
                ).casefold(),
            }
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(
            f"coarse calibration local-schedule preflight failed; report: {report_path}"
        ) from error

    first_manifest_evidence: dict[str, str] | None = None
    first_manifest: dict[str, Any] | None = None
    if point_index == 0 and args.first_manifest is not None:
        report = _coarse_calibration_failure_report(
            stage="FIRST_POINT",
            code="FIRST_MANIFEST_NOT_ALLOWED",
            message="the first configured point must not use --first-manifest",
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(f"first-point preflight failed; report: {report_path}")
    if point_index == 1:
        if args.first_manifest is None:
            report = _coarse_calibration_failure_report(
                stage="SECOND_POINT",
                code="FIRST_MANIFEST_REQUIRED",
                message="the second configured point requires --first-manifest",
                config_path=config_path,
                output_directory=lexical_output,
                characteristic_length_m=characteristic_length,
                controller_argv=list(args.controller_argv),
            )
            report_path = _write_coarse_calibration_preflight(
                evidence_directory, report
            )
            raise ValueError(
                f"second-point preflight failed; report: {report_path}"
            )
        try:
            first_manifest, first_manifest_evidence = (
                _load_first_coarse_calibration_manifest(
                    args.first_manifest,
                    output_root=lexical_root,
                    expected_config_sha256=config_sha256,
                    contract=contract,
                    expected_characteristic_length_m=configured_sizes[0],
                    projection_evidence=projection_evidence,
                    local_schedule_binding=local_schedule_binding,
                )
            )
        except (OSError, ValueError) as error:
            report = _coarse_calibration_failure_report(
                stage="SECOND_POINT",
                code="FIRST_MANIFEST_INVALID",
                message=str(error),
                config_path=config_path,
                output_directory=lexical_output,
                characteristic_length_m=characteristic_length,
                controller_argv=list(args.controller_argv),
            )
            report_path = _write_coarse_calibration_preflight(
                evidence_directory, report
            )
            raise ValueError(
                f"second-point manifest preflight failed; report: {report_path}"
            ) from error

    try:
        system_snapshot = capture_system_snapshot(evidence_directory)
    except (OSError, CoarseCalibrationGateError) as error:
        report = _coarse_calibration_failure_report(
            stage="FIRST_POINT" if point_index == 0 else "SECOND_POINT",
            code="SYSTEM_SNAPSHOT_FAILED",
            message=str(error),
            config_path=config_path,
            output_directory=lexical_output,
            characteristic_length_m=characteristic_length,
            controller_argv=list(args.controller_argv),
        )
        report_path = _write_coarse_calibration_preflight(
            evidence_directory, report
        )
        raise ValueError(
            f"coarse calibration resource snapshot failed; report: {report_path}"
        ) from error

    if point_index == 0:
        preflight = evaluate_first_point_preflight(
            contract, system_snapshot, timeout_seconds=timeout
        )
    else:
        if first_manifest is None:
            raise AssertionError("second-point manifest preflight was bypassed")
        preflight = evaluate_second_point_preflight(
            contract,
            system_snapshot,
            first_manifest,
            timeout_seconds=timeout,
            second_characteristic_length_m=characteristic_length,
        )
        preflight["inputs"]["first_manifest_evidence"] = first_manifest_evidence
    preflight["policy"]["command_runner_constructed"] = False
    preflight["inputs"].update(
        {
            "config_path": str(config_path),
            "config_sha256": config_sha256,
            "output_directory": str(lexical_output),
            "selected_characteristic_length_m": characteristic_length,
            "local_schedule_path": str(local_schedule_path),
            "local_schedule_sha256": local_schedule_sha256,
            "local_schedule_binding_sha256": local_schedule_binding.get(
                "binding_sha256"
            ),
            "projection_manifest_path": projection_evidence["path"],
            "projection_manifest_sha256": projection_evidence["sha256"],
            "projection_evidence_sha256": projection_evidence[
                "evidence_sha256"
            ],
            "projection_projected_3d_elements": projection_evidence[
                "projected_3d_elements"
            ],
            "controller_argv": list(args.controller_argv),
        }
    )
    if preflight.get("status") != "PASS":
        preflight_path = _write_coarse_calibration_preflight(
            evidence_directory, preflight
        )
        raise ValueError(
            f"coarse calibration resource preflight failed; report: {preflight_path}"
        )

    runner = CommandRunner(evidence_directory, live_output=not args.quiet)
    preflight["policy"]["command_runner_constructed"] = True
    preflight_path = _write_coarse_calibration_preflight(
        evidence_directory, preflight
    )
    worker_arguments = (
        "-m",
        "cfdpipe.coarse_mesh_worker",
        "--config",
        str(config_path),
        "--config-sha256",
        config_sha256,
        "--coarse-contract-sha256",
        str(contract["normalized_config_sha256"]),
        "--characteristic-length",
        format(characteristic_length, ".17g"),
        "--projection-manifest",
        str(Path(projection_evidence["path"])),
        "--projection-sha256",
        str(projection_evidence["sha256"]),
        "--output",
        str(lexical_output),
        "--local-schedule",
        str(local_schedule_path),
        "--local-schedule-sha256",
        local_schedule_sha256,
    )
    abort_check = _make_physical_memory_abort_check(contract)
    try:
        result = runner.run(
            python_executable,
            worker_arguments,
            cwd=repository_root,
            env={"PYTHONPATH": str(source_root)},
            timeout=timeout,
            check=True,
            live_output=not args.quiet,
            name="coarse-mesh-calibration-worker",
            stdout_log="worker.stdout.log",
            stderr_log="worker.stderr.log",
            metadata_log="worker.command.json",
            abort_check=abort_check,
            abort_check_interval_seconds=0.5,
        )
    except CommandExecutionError as error:
        raise CommandExecutionError(
            error.result,
            "coarse calibration worker failed; command evidence remains in "
            f"{evidence_directory}",
        ) from error

    manifest_path = lexical_output / "coarse_calibration_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("coarse calibration worker returned zero without a manifest")

    def reject_json_constant(value: str) -> None:
        raise ValueError(f"coarse calibration manifest contains {value}")

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            parse_constant=reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read coarse calibration manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("coarse calibration manifest must be a JSON object")
    _validate_coarse_calibration_pass_manifest(
        manifest,
        manifest_path=manifest_path,
        expected_output_directory=resolved_output,
        expected_config_sha256=config_sha256,
        contract=contract,
        expected_characteristic_length_m=characteristic_length,
        projection_evidence=projection_evidence,
        local_schedule_binding=local_schedule_binding,
    )
    _print_json(
        {
            "status": "PASS",
            "output_directory": str(lexical_output),
            "manifest": str(manifest_path),
            "worker_evidence_directory": str(evidence_directory),
            "preflight": str(preflight_path),
            "worker_metadata": str(evidence_directory / "worker.command.json"),
            "worker_command": result.as_metadata(),
        }
    )
    return 0


def _handle_pipeline_rans_smoke(args: argparse.Namespace) -> int:
    if args.nproc != 1:
        raise ValueError("RANS smoke requires --nproc 1")
    if not 20 <= args.max_iterations <= 500:
        raise ValueError("RANS smoke --max-iterations must be between 20 and 500")
    if not math.isfinite(args.timeout) or not 0.0 < args.timeout <= 840.0:
        raise ValueError("RANS smoke --timeout must be in (0, 840]")
    if args.spatial_order == 1 and args.restart_manifest is not None:
        raise ValueError("--restart-manifest is only valid with --spatial-order 2")
    if args.spatial_order == 2 and args.restart_manifest is None:
        raise ValueError("--spatial-order 2 requires --restart-manifest")
    report = run_rans_smoke_validation(
        step_path=Path(os.path.abspath(args.step.expanduser())),
        project_path=Path(os.path.abspath(args.project.expanduser())),
        cases_path=Path(os.path.abspath(args.cases.expanduser())),
        markers_path=Path(os.path.abspath(args.markers.expanduser())),
        topology_smoke_path=Path(
            os.path.abspath(args.topology_smoke_config.expanduser())
        ),
        boundary_layer_trial_path=Path(
            os.path.abspath(args.boundary_layer_trial_config.expanduser())
        ),
        boundary_layer_smoke_path=Path(
            os.path.abspath(args.boundary_layer_smoke_config.expanduser())
        ),
        rear_outlet_pilot_path=Path(
            os.path.abspath(args.rear_outlet_pilot_config.expanduser())
        ),
        rans_smoke_path=Path(os.path.abspath(args.rans_config.expanduser())),
        mesh_manifest_path=Path(os.path.abspath(args.mesh_manifest.expanduser())),
        outlet_evidence_path=Path(os.path.abspath(args.outlet_evidence.expanduser())),
        case_id=args.case_id,
        max_iterations=args.max_iterations,
        output_directory=Path(os.path.abspath(args.output.expanduser())),
        trusted_repository_root=REPOSITORY_ROOT,
        toolchain=_toolchain(args),
        paraview_script_directory=DEFAULT_PARAVIEW_SCRIPTS,
        timeout_seconds=args.timeout,
        live_output=not args.quiet,
        controller_argv=getattr(args, "controller_argv", ()),
        spatial_order=args.spatial_order,
        restart_manifest_path=None
        if args.restart_manifest is None
        else Path(os.path.abspath(args.restart_manifest.expanduser())),
    )
    _print_json(report)
    return 0


def _handle_pipeline_rear_outlet_freeze_check(args: argparse.Namespace) -> int:
    raw_output = args.output.expanduser()
    if ".." in raw_output.parts:
        raise ValueError("rear-outlet freeze --output must not contain '..'")
    lexical_output = Path(os.path.abspath(raw_output))
    lexical_root = Path(os.path.abspath(DEFAULT_REAR_OUTLET_FREEZE_OUTPUT_ROOT))
    resolved_output = lexical_output.resolve(strict=False)
    resolved_root = lexical_root.resolve(strict=False)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    if resolved_output == resolved_root or resolved_root not in resolved_output.parents:
        raise ValueError(
            f"rear-outlet freeze output must be a child of {lexical_root}"
        )
    if repository_root not in resolved_output.parents:
        raise ValueError("rear-outlet freeze output resolves outside the repository")
    if resolved_output.exists() and not resolved_output.is_dir():
        raise ValueError("rear-outlet freeze output must be a directory")

    report_path = resolved_output / "rear_outlet_freeze_report.json"
    if report_path.is_symlink():
        raise ValueError("rear-outlet freeze report path must not be a symbolic link")
    if report_path.exists():
        raise ValueError(
            "rear-outlet freeze report already exists; use a new run directory"
        )
    config_path = Path(os.path.abspath(args.config.expanduser())).resolve(strict=False)
    report = evaluate_rear_outlet_freeze(config_path, report_path)
    if not isinstance(report, dict):
        raise ValueError("rear-outlet freeze evaluator returned an invalid report")
    _print_json(report)
    return 0 if report.get("status") == "PASS" else 1


def _publish_boundary_layer_smoke_preflight_failure(
    args: argparse.Namespace, error: BaseException
) -> None:
    output_value = getattr(args, "output", None)
    if not isinstance(output_value, Path) or ".." in output_value.expanduser().parts:
        return
    output = Path(os.path.abspath(output_value.expanduser()))
    resolved_output = output.resolve(strict=False)
    roots = [
        Path(os.path.abspath(DEFAULT_BOUNDARY_LAYER_SMOKE_OUTPUT_ROOT)),
        Path(os.path.abspath(DEFAULT_BOUNDARY_LAYER_QUALITY_OUTPUT_ROOT)),
    ]
    resolved_roots = [value.resolve(strict=False) for value in roots]
    if not any(
        resolved_output != root and root in resolved_output.parents
        for root in resolved_roots
    ):
        return
    if output.exists():
        return
    try:
        output.mkdir(parents=True, exist_ok=False)
        ended = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rendered = traceback.format_exc()
        evidence = {
            "schema": "cfdpipe.boundary_layer_smoke.preflight.v1",
            "status": "FAIL",
            "phase": "python_preflight",
            "ended_at_utc": ended,
            "controller_argv": list(getattr(args, "controller_argv", ())),
            "output_directory": str(output),
            "gmsh_initialized": False,
            "su2_called": False,
            "paraview_called": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": rendered,
            },
        }
        with (output / "preflight_manifest.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            json.dump(evidence, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        with (output / "preflight.log").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(rendered)
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.controller_argv = list(
        sys.argv if argv is None else ["cfdpipe", *argv]
    )
    try:
        return int(args.handler(args))
    except (
        ToolchainError,
        CommandExecutionError,
        GmshBridgeError,
        ParaViewBridgeError,
        SU2BridgeError,
        ConnectionTestError,
        RealConnectionError,
        GeometryEvidenceReviewError,
        GeometryRepairError,
        InterfacePersistenceError,
        MarkerConfigError,
        OutletValidationPipelineError,
        BoundaryLayerTrialError,
        BoundaryLayerSmokeError,
        BoundaryLayerClearanceError,
        BoundaryLayerLocalScheduleError,
        RANSSmokePipelineError,
        PipelineStageError,
    ) as exc:
        if getattr(args.handler, "__name__", "") == "_handle_pipeline_boundary_layer_smoke":
            _publish_boundary_layer_smoke_preflight_failure(args, exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ImportError, NotImplementedError, OSError, ValueError) as exc:
        if getattr(args.handler, "__name__", "") == "_handle_pipeline_boundary_layer_smoke":
            _publish_boundary_layer_smoke_preflight_failure(args, exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
