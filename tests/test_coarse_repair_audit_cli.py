from __future__ import annotations

import copy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cfdpipe.cli as cli_module
from cfdpipe.coarse_direction_feasibility import (
    make_owner_free_direction_endpoint,
    make_owner_free_direction_pattern_endpoint,
)
from cfdpipe.coarse_schedule_feasibility import (
    apply_minimum_growth_endpoint,
    make_minimum_growth_endpoint,
)


class _AuditRunner:
    manifest_factory = None
    returncode = 0
    instances: list["_AuditRunner"] = []

    def __init__(self, output_dir, *, live_output=False, **kwargs) -> None:
        self.output_dir = Path(output_dir)
        self.live_output = live_output
        self.kwargs = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, executable, args=(), **kwargs):
        arguments = tuple(str(value) for value in args)
        self.calls.append((str(executable), arguments, kwargs))
        output = Path(arguments[arguments.index("--output") + 1])
        output.mkdir(parents=True)
        factory = self.__class__.manifest_factory
        if factory is None:
            raise AssertionError("audit manifest factory is missing")
        manifest = factory(output, arguments)
        encoded = (
            manifest
            if isinstance(manifest, str)
            else json.dumps(manifest, allow_nan=False)
        )
        (output / "coarse_repair_audit_manifest.json").write_text(
            encoded, encoding="utf-8"
        )
        return SimpleNamespace(
            returncode=self.__class__.returncode,
            timed_out=False,
            stderr="",
            executable=str(executable),
            as_metadata=lambda: {
                "executable": str(executable),
                "args": list(arguments),
                "cwd": str(kwargs["cwd"]),
                "returncode": self.__class__.returncode,
            },
        )


class CoarseRepairAuditCliTests(unittest.TestCase):
    def setUp(self) -> None:
        _AuditRunner.instances.clear()
        _AuditRunner.returncode = 0
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src").mkdir()
        self.gmsh_module = self.root / "src" / "gmsh.py"
        self.gmsh_module.write_text("# test gmsh module identity\n", encoding="utf-8")
        (self.root / "config").mkdir()
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.config.write_text('schema = "test"\n', encoding="utf-8")
        self.config_sha256 = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.output_root.mkdir(parents=True)
        self.output = self.output_root / "repair_audit_001"
        self.direction_sources = []
        for label in ("primary", "confirmation"):
            source_manifest = (
                self.output_root
                / f"direction-audit-{label}"
                / "coarse_repair_audit_manifest.json"
            )
            source_manifest.parent.mkdir()
            source_manifest.write_text(
                json.dumps({"source": label}, allow_nan=False),
                encoding="utf-8",
            )
            self.direction_sources.append(
                (
                    source_manifest,
                    hashlib.sha256(source_manifest.read_bytes()).hexdigest(),
                )
            )
        self.projection = (
            self.output_root / "projection_001" / "projection_manifest.json"
        )
        self.projection.parent.mkdir()
        self.projection.write_text("{}\n", encoding="utf-8")
        self.projection_sha256 = hashlib.sha256(
            self.projection.read_bytes()
        ).hexdigest()
        self.schedule = (
            self.output_root
            / "schedule_001"
            / "boundary_layer_local_schedule.json"
        )
        self.schedule.parent.mkdir()
        self.schedule.write_text("{}\n", encoding="utf-8")
        self.schedule_sha256 = hashlib.sha256(self.schedule.read_bytes()).hexdigest()
        self.source = self.root / "geometry" / "derived" / "pipeline.brep"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("BREP test source\n", encoding="utf-8")
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.source.chmod(stat.S_IREAD)

        def restore_source_write_permission() -> None:
            if self.source.exists():
                self.source.chmod(stat.S_IREAD | stat.S_IWRITE)

        self.addCleanup(restore_source_write_permission)
        self.contract_sha256 = "a" * 64
        self.first_layer_height_m = 2.764577127353465e-7
        self.layer_count = 60
        self.wall_fingerprints = [f"{index:064x}" for index in range(48)]
        self.surface_schedules = {
            fingerprint: [
                self.first_layer_height_m
                * sum(1.2**power for power in range(layer))
                for layer in range(1, self.layer_count + 1)
            ]
            for fingerprint in self.wall_fingerprints
        }
        self.contract = {
            "normalized_config_sha256": self.contract_sha256,
            "pipeline_brep_path": str(self.source.resolve()),
            "provenance": {"pipeline_brep_sha256": self.source_sha256},
            "calibration_characteristic_lengths_m": [0.40, 0.32],
            "boundary_layer_design": {
                "first_layer_height_m": self.first_layer_height_m,
                "layer_count": self.layer_count,
            },
            "quality": {
                "minimum_prism_scaled_jacobian": 0.01,
                "minimum_core_tetra_gamma": 0.001,
            },
            "wall_surface_fingerprints": self.wall_fingerprints,
            "resource": {
                "projection_worker_memory_limit_bytes": 2 * 1024**3,
                "calibration_worker_memory_limit_bytes": 3 * 1024**3,
                "os_physical_memory_reserve_bytes": 4 * 1024**3,
                "worker_monitor_margin_bytes": 512 * 1024**2,
            },
        }
        self.replay_approval = {
            "approval_sha256": "c" * 64,
            "local_schedule_binding_sha256": "b" * 64,
            "candidate_root_count": 16,
            "changed_root_count": 10,
            "changed_roots": [],
            "root_contexts": [],
            "source_minimum_growth_final_quality": {
                "prism_element_count": 280_320,
                "core_element_count": 13_739,
            },
            "source_audit_manifests": [
                {
                    "path": str(path.resolve()),
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                }
                for path, digest in self.direction_sources
            ],
        }
        self.homotopy_audit_evidence = None
        self.schedule_direction_continuation_endpoint = None
        self.projection_evidence = {
            "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
            "status": "PASS",
            "path": str(self.projection.resolve()),
            "sha256": self.projection_sha256,
            "size_bytes": self.projection.stat().st_size,
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract_sha256,
            "characteristic_length_m": 0.40,
            "projected_3d_elements": 294_059,
            "evidence_sha256": "e" * 64,
        }
        self.schedule_binding = {
            "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
            "status": "PASS",
            "source_plan_path": str(self.schedule.resolve()),
            "source_plan_sha256": self.schedule_sha256,
            "source_plan_size_bytes": self.schedule.stat().st_size,
            "coarse_contract_sha256": self.contract_sha256,
            "surface_count": 48,
            "layer_count": 60,
            "binding_sha256": "b" * 64,
            "surface_schedules": {
                fingerprint: {
                    "surface_fingerprint_id": fingerprint,
                    "layer_count": self.layer_count,
                    "cumulative_heights_m": list(values),
                }
                for fingerprint, values in self.surface_schedules.items()
            },
        }
        patches = (
            mock.patch.object(cli_module, "REPOSITORY_ROOT", self.root),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_CONFIG", self.config
            ),
            mock.patch.object(
                cli_module, "DEFAULT_COARSE_MESH_OUTPUT_ROOT", self.output_root
            ),
            mock.patch.object(
                cli_module,
                "normalize_coarse_mesh_config",
                return_value=self.contract,
            ),
            mock.patch.object(
                cli_module,
                "load_and_validate_projection_evidence",
                return_value=self.projection_evidence,
            ),
            mock.patch.object(
                cli_module,
                "validate_projection_evidence_record",
                side_effect=lambda record, **kwargs: dict(record),
            ),
            mock.patch.object(
                cli_module,
                "_load_coarse_local_schedule_binding",
                return_value=(
                    self.schedule.resolve(),
                    self.schedule_sha256,
                    self.schedule_binding,
                ),
            ),
            mock.patch.object(
                cli_module,
                "capture_system_snapshot",
                return_value={"snapshot": "test"},
            ),
            mock.patch.object(
                cli_module,
                "evaluate_first_point_preflight",
                return_value={
                    "schema": "test.preflight",
                    "status": "PASS",
                    "policy": {},
                    "inputs": {},
                    "requirements": {},
                    "checks": {},
                    "reasons": [],
                },
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        _AuditRunner.manifest_factory = self._manifest

    def arguments(
        self,
        *,
        characteristic: str = "0.40",
        schedule_endpoint: str | None = None,
        direction_replay: bool = False,
    ) -> list[str]:
        arguments = [
            "pipeline",
            "coarse-repair-audit",
            "--config",
            str(self.config),
            "--characteristic-length",
            characteristic,
            "--projection-manifest",
            str(self.projection),
            "--projection-sha256",
            self.projection_sha256,
            "--local-schedule",
            str(self.schedule),
            "--local-schedule-sha256",
            self.schedule_sha256,
        ]
        if schedule_endpoint is not None:
            arguments.extend(
                ["--schedule-feasibility-endpoint", schedule_endpoint]
            )
        if direction_replay:
            arguments.extend(
                [
                    "--direction-audit-primary",
                    str(self.direction_sources[0][0]),
                    "--direction-audit-primary-sha256",
                    self.direction_sources[0][1],
                    "--direction-audit-confirmation",
                    str(self.direction_sources[1][0]),
                    "--direction-audit-confirmation-sha256",
                    self.direction_sources[1][1],
                ]
            )
        arguments.extend(
            [
                "--output",
                str(self.output),
                "--timeout",
                "300",
                "--quiet",
            ]
        )
        return arguments

    def _persist_isolation(self, output: Path, manifest: dict) -> None:
        path = (
            output.parent
            / "_worker_evidence"
            / output.name
            / "repair_audit_worker.json"
        )
        path.write_text(
            json.dumps(manifest["worker_isolation"], allow_nan=False),
            encoding="utf-8",
        )

    def _replay_patches(
        self,
        *,
        consensus_error: BaseException | None = None,
        physical_validator=None,
        homotopy_validator=None,
    ) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(
                cli_module,
                "make_coarse_repair_audit_strategy_config",
                return_value={"strategy": "minimum-growth-pattern"},
            )
        )
        consensus_patch = mock.patch.object(
            cli_module,
            "load_direction_replay_consensus_approval",
            side_effect=(
                consensus_error
                if consensus_error is not None
                else lambda *args, **kwargs: copy.deepcopy(
                    self.replay_approval
                )
            ),
        )
        stack.enter_context(consensus_patch)
        stack.enter_context(
            mock.patch.object(
                cli_module,
                "validate_direction_replay_approval",
                side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
            )
        )
        stack.enter_context(
            mock.patch.object(
                cli_module,
                "validate_physical_schedule_replay_discovery",
                new=(
                    physical_validator
                    if physical_validator is not None
                    else lambda value, **kwargs: copy.deepcopy(dict(value))
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                cli_module,
                "validate_physical_schedule_homotopy_discovery",
                new=(
                    homotopy_validator
                    if homotopy_validator is not None
                    else lambda value, **kwargs: copy.deepcopy(dict(value))
                ),
            )
        )
        return stack

    def _manifest(self, output: Path, arguments: tuple[str, ...]) -> dict:
        physical_replay = "--direction-audit-primary" in arguments
        manifest_status = (
            "INCOMPLETE"
            if physical_replay and _AuditRunner.returncode == 2
            else "PASS"
        )
        endpoint_requested = (
            arguments[arguments.index("--schedule-feasibility-endpoint") + 1]
            if "--schedule-feasibility-endpoint" in arguments
            else None
        )
        physical_homotopy = (
            physical_replay
            and endpoint_requested == cli_module.PHYSICAL_HOMOTOPY_REQUEST
        )
        direction_continuation = (
            physical_replay
            and endpoint_requested == cli_module.SCHEDULE_CONTINUATION_REQUEST
        )
        combined_physical = physical_homotopy or direction_continuation
        direction_family = endpoint_requested in {
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
        }
        wall_inventory = [f"{index:064x}" for index in range(48)]
        counts = {
            "initial_bad_prism_count": 14,
            "negative_volume_prism_count": 8,
            "cone_node_count": 13,
            "full_layer_bad_prism_count": 0,
            "initial_below_threshold_prism_count": 360,
            "repair_candidate_prism_count": 360,
            "bad_source_triangle_count": 0,
            "strict_bad_source_triangle_count": 0,
            "preferred_strict_failure_root_count": 0,
            "smoothing_group_count": 0,
            "candidate_smoothed_root_count": 0,
            "owner_ambiguity_count": 0,
            "source_prism_count": 4672,
            "projected_3d_element_count": 294_059,
            "changed_smoothed_root_count": 0,
        }
        initial_bad = [
            {
                "source_triangle_sha256": f"{100 + index:064x}",
                "wall_surface_fingerprint": wall_inventory[0],
            }
            for index in range(counts["initial_bad_prism_count"])
        ]
        reversed_prisms = [
            {
                "source_triangle_sha256": f"{200 + index:064x}",
                "wall_surface_fingerprint": wall_inventory[0],
            }
            for index in range(counts["negative_volume_prism_count"])
        ]
        signature = {
            "observed_counts": counts,
            "wall_surface_fingerprint_inventory": wall_inventory,
            "initial_bad_one_layer_prisms": initial_bad,
            "reversed_one_layer_prisms": reversed_prisms,
            "repair_sites": [],
        }
        strategy = {
            "repair_audit_only": True,
            "contract_mode": "coarse_repair_audit_only",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
        }
        endpoint = None
        endpoint_application = None
        if combined_physical:
            endpoint = cli_module.make_physical_schedule_homotopy_endpoint(
                baseline_binding_sha256=self.schedule_binding[
                    "binding_sha256"
                ],
                first_layer_height_m=self.first_layer_height_m,
                layer_count=self.layer_count,
            )
        elif endpoint_requested is not None:
            endpoint = make_minimum_growth_endpoint(
                baseline_binding_sha256=self.schedule_binding[
                    "binding_sha256"
                ],
                first_layer_height_m=self.first_layer_height_m,
                layer_count=self.layer_count,
            )
            _, endpoint_application = apply_minimum_growth_endpoint(
                self.surface_schedules,
                endpoint,
                expected_surface_fingerprints=self.wall_fingerprints,
            )
            strategy.update(
                {
                    "schedule_feasibility_endpoint": endpoint,
                    "audit_purpose": (
                        "minimum_growth_plus_existing_direction_repair"
                        if endpoint_requested
                        == "minimum-growth-existing-direction-repair"
                        else (
                            "minimum_growth_owner_free_component_direction_audit"
                            if endpoint_requested
                            == "minimum-growth-owner-free-component-direction-audit"
                            else "minimum_growth_owner_free_component_pattern_audit"
                        )
                    ),
                    "combined_endpoint_only": True,
                }
            )
            if direction_family:
                factory = (
                    make_owner_free_direction_endpoint
                    if endpoint_requested
                    == "minimum-growth-owner-free-component-direction-audit"
                    else make_owner_free_direction_pattern_endpoint
                )
                strategy["owner_free_direction_endpoint"] = factory(
                    schedule_endpoint_sha256=endpoint["endpoint_sha256"]
                )
        if physical_replay:
            strategy.update(
                {
                    "audit_purpose": (
                        cli_module.SCHEDULE_CONTINUATION_PURPOSE
                        if direction_continuation
                        else (
                            cli_module.PHYSICAL_HOMOTOPY_PURPOSE
                            if physical_homotopy
                            else "physical_local_schedule_fixed_direction_replay"
                        )
                    ),
                    "combined_endpoint_only": combined_physical,
                    "fixed_direction_replay_only": True,
                    "owner_free_direction_replay_approval": copy.deepcopy(
                        self.replay_approval
                    ),
                }
            )
            if combined_physical:
                strategy.update(
                    {
                        "schedule_feasibility_endpoint": copy.deepcopy(
                            endpoint
                        ),
                        "physical_schedule_homotopy_endpoint": copy.deepcopy(
                            endpoint
                        ),
                    }
                )
                if physical_homotopy:
                    strategy["fixed_direction_homotopy_only"] = True
                else:
                    strategy.update(
                        {
                            "first_frontier_direction_continuation_only": True,
                            "schedule_direction_continuation_endpoint": (
                                copy.deepcopy(
                                    self.schedule_direction_continuation_endpoint
                                )
                            ),
                        }
                    )
        strategy_sha256 = cli_module._canonical_mapping_sha256(strategy)
        strategy["normalized_config_sha256"] = strategy_sha256
        evidence_path = (
            output.parent
            / "_worker_evidence"
            / output.name
            / "repair_audit_worker.json"
        )
        worker_pid = 4242

        def memory_base(schema: str, operation: str, captured: str) -> dict:
            return {
                "schema": schema,
                "status": "PASS",
                "supported": True,
                "platform": "win32",
                "operation": operation,
                "process_id": worker_pid,
                "captured_at_utc": captured,
                "error": None,
            }

        def system_memory(captured: str) -> dict:
            record = memory_base(
                cli_module.SYSTEM_REPORT_SCHEMA,
                "capture_system_physical_memory",
                captured,
            )
            record["metrics"] = {
                "physical_total_bytes": 16 * 1024**3,
                "physical_available_bytes": 8 * 1024**3,
                "pagefile_total_bytes": 24 * 1024**3,
                "pagefile_available_bytes": 12 * 1024**3,
                "virtual_total_bytes": 128 * 1024**4,
                "virtual_available_bytes": 127 * 1024**4,
                "memory_load_percent": 50,
            }
            return record

        def process_memory(captured: str) -> dict:
            record = memory_base(
                cli_module.PROCESS_REPORT_SCHEMA,
                "capture_current_process_memory",
                captured,
            )
            record["metrics"] = {
                "working_set_bytes": 100 * 1024**2,
                "peak_working_set_bytes": 120 * 1024**2,
                "private_usage_bytes": 80 * 1024**2,
                "pagefile_usage_bytes": 80 * 1024**2,
                "peak_pagefile_usage_bytes": 90 * 1024**2,
            }
            record["private_commit_semantics"] = {
                "schema": cli_module.PRIVATE_COMMIT_SEMANTICS_SCHEMA,
                "job_object_process_memory_limit_basis": "private_commit_bytes",
                "current_private_commit_metric": "metrics.private_usage_bytes",
                "current_private_commit_source": (
                    cli_module.PRIVATE_COMMIT_CURRENT_SOURCE
                ),
                "peak_private_commit_metric": (
                    "metrics.peak_pagefile_usage_bytes"
                ),
                "peak_private_commit_source": cli_module.PRIVATE_COMMIT_PEAK_SOURCE,
                "pagefile_field_means_commit_charge_not_pagefile_residency": True,
                "working_set_is_not_job_process_memory_limit_metric": True,
            }
            return record

        hard_limit = memory_base(
            cli_module.LIMIT_REPORT_SCHEMA,
            "install_current_process_memory_limit",
            "2026-08-02T00:00:00.300000Z",
        )
        hard_limit.update(
            {
                "requested_process_memory_limit_bytes": 3 * 1024**3,
                "backend": "windows_job_object",
                "job_object": {
                    "job_handle_value": 99,
                    "limit_flags": (
                        cli_module.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                        | cli_module.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    ),
                    "process_memory_limit_bytes": 3 * 1024**3,
                    "process_assigned": True,
                    "handle_retained_for_process_lifetime": True,
                },
            }
        )
        isolation = {
            "schema": "cfdpipe.coarse_repair_audit_worker.v1",
            "status": "PASS",
            "audit_only": True,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "started_at_utc": "2026-08-02T00:00:00Z",
            "ended_at_utc": "2026-08-02T00:00:01Z",
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract_sha256,
            "projection_evidence": self.projection_evidence,
            "local_schedule": {
                "pre_job_validation": {
                    "path": str(self.schedule.resolve()),
                    "sha256": self.schedule_sha256,
                    "size_bytes": self.schedule.stat().st_size,
                    "validated_before_job_object": True,
                    "regular_non_link_json": True,
                    "inside_runs_mesh_coarse": True,
                    "outside_current_output_and_evidence": True,
                },
                "binding": self.schedule_binding,
            },
            "direction_replay": {
                "pre_job_sources": (
                    [
                        {
                            "path": str(path.resolve()),
                            "sha256": digest,
                            "size_bytes": path.stat().st_size,
                        }
                        for path, digest in self.direction_sources
                    ]
                    if physical_replay
                    else None
                ),
                "approval": (
                    copy.deepcopy(self.replay_approval)
                    if physical_replay
                    else None
                ),
                "homotopy_endpoint": (
                    copy.deepcopy(endpoint) if combined_physical else None
                ),
                "homotopy_audit_source": (
                    {
                        key: self.homotopy_audit_evidence[key]
                        for key in (
                            "path",
                            "sha256",
                            "size_bytes",
                            "manifest_status",
                        )
                    }
                    if direction_continuation
                    else None
                ),
                "schedule_direction_continuation_endpoint": (
                    copy.deepcopy(
                        self.schedule_direction_continuation_endpoint
                    )
                    if direction_continuation
                    else None
                ),
            },
            "worker": {
                "argv": [str(Path(sys.executable).resolve()), *arguments],
                "config_path": str(self.config.resolve()),
                "output_directory": str(output.resolve()),
                "manifest_path": str(
                    (output / "coarse_repair_audit_manifest.json").resolve()
                ),
                "evidence_path": str(evidence_path.resolve()),
                "characteristic_length_m": 0.40,
                "configured_process_memory_limit_bytes": 3 * 1024**3,
                "hard_limit_installed_before_heavy_import": True,
                "heavy_dependencies_imported": True,
                "normalized_resource_matches_raw": True,
                "local_schedule_bound_after_heavy_import": True,
                "audit_runner_called": True,
            },
            "worker_memory": {
                "system_before_limit": system_memory(
                    "2026-08-02T00:00:00.100000Z"
                ),
                "process_before_limit": process_memory(
                    "2026-08-02T00:00:00.200000Z"
                ),
                "hard_limit": hard_limit,
                "system_after_limit": system_memory(
                    "2026-08-02T00:00:00.400000Z"
                ),
                "process_after_limit": process_memory(
                    "2026-08-02T00:00:00.500000Z"
                ),
                "system_after_audit": system_memory(
                    "2026-08-02T00:00:00.900000Z"
                ),
                "process_after_audit": process_memory(
                    "2026-08-02T00:00:00.800000Z"
                ),
            },
            "audit_result_status": manifest_status,
            "worker_error": None,
        }
        evidence_path.write_text(
            json.dumps(isolation, allow_nan=False), encoding="utf-8"
        )
        run_evidence = {
            "worker_argv": [str(Path(sys.executable).resolve()), *arguments],
            "config_path": str(self.config.resolve()),
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract_sha256,
        }
        if endpoint_requested is not None:
            run_evidence["schedule_feasibility_endpoint"] = endpoint_requested
        if physical_replay:
            run_evidence.update(
                {
                    "direction_audit_primary_path": str(
                        self.direction_sources[0][0].resolve()
                    ),
                    "direction_audit_primary_sha256": self.direction_sources[
                        0
                    ][1],
                    "direction_audit_confirmation_path": str(
                        self.direction_sources[1][0].resolve()
                    ),
                    "direction_audit_confirmation_sha256": (
                        self.direction_sources[1][1]
                    ),
                    "direction_replay_approval_sha256": (
                        self.replay_approval["approval_sha256"]
                    ),
                }
            )
            if combined_physical:
                run_evidence[
                    "physical_schedule_homotopy_endpoint_sha256"
                ] = endpoint["endpoint_sha256"]
            if direction_continuation:
                run_evidence.update(
                    {
                        "homotopy_audit_manifest_path": (
                            self.homotopy_audit_evidence["path"]
                        ),
                        "homotopy_audit_manifest_sha256": (
                            self.homotopy_audit_evidence["sha256"]
                        ),
                        "schedule_direction_continuation_endpoint_sha256": (
                            self.schedule_direction_continuation_endpoint[
                                "endpoint_sha256"
                            ]
                        ),
                    }
                )
        manifest = {
            "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
            "status": manifest_status,
            "audit_only": True,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "error": None,
            "audit_purpose": (
                cli_module.SCHEDULE_CONTINUATION_PURPOSE
                if direction_continuation
                else cli_module.PHYSICAL_HOMOTOPY_PURPOSE
                if physical_homotopy
                else "physical_local_schedule_fixed_direction_replay"
                if physical_replay
                else "repair_profile_discovery"
                if endpoint_requested is None
                else (
                    "minimum_growth_plus_existing_direction_repair"
                    if endpoint_requested
                    == "minimum-growth-existing-direction-repair"
                    else (
                        "minimum_growth_owner_free_component_direction_audit"
                        if endpoint_requested
                        == "minimum-growth-owner-free-component-direction-audit"
                        else "minimum_growth_owner_free_component_pattern_audit"
                    )
                )
            ),
            "schedule_feasibility_endpoint_requested": endpoint_requested,
            "direction_replay_approval": (
                copy.deepcopy(self.replay_approval)
                if physical_replay
                else None
            ),
            "physical_schedule_homotopy_endpoint": (
                copy.deepcopy(endpoint) if combined_physical else None
            ),
            "schedule_direction_continuation_endpoint": (
                copy.deepcopy(self.schedule_direction_continuation_endpoint)
                if direction_continuation
                else None
            ),
            "homotopy_audit_source": (
                {
                    key: self.homotopy_audit_evidence[key]
                    for key in (
                        "path",
                        "sha256",
                        "size_bytes",
                        "manifest_status",
                    )
                }
                if direction_continuation
                else None
            ),
            "started_at_utc": "2026-08-02T00:00:00Z",
            "ended_at_utc": "2026-08-02T00:00:01Z",
            "elapsed_seconds": 1.0,
            "source_unchanged": True,
            "source_before": {
                "path": str(self.source.resolve()),
                "sha256": self.source_sha256,
                "size_bytes": self.source.stat().st_size,
                "read_only": True,
            },
            "source_after": {
                "path": str(self.source.resolve()),
                "sha256": self.source_sha256,
                "size_bytes": self.source.stat().st_size,
                "read_only": True,
            },
            "coarse_contract_sha256": self.contract_sha256,
            "characteristic_length_m": 0.40,
            "gmsh_version": "4.13.1-test",
            "gmsh_module_path": str(self.gmsh_module.resolve()),
            "run_evidence": run_evidence,
            "gmsh_session": {
                "initialize_attempted": True,
                "initialize_called": True,
                "logger_start_attempted": True,
                "logger_started": True,
                "logger_get_attempted": True,
                "logger_capture_succeeded": True,
                "logger_messages": [],
                "logger_stop_attempted": True,
                "logger_stopped": True,
                "diagnostic_audit": {
                    "status": "PASS",
                    "raw_messages_preserved": True,
                    "fatal_messages": [],
                    "pending_diagnostics": [],
                    "classified_nonfatal_diagnostics": [],
                    "associated_summary_lines": [],
                    "policy": "test strict logger policy",
                },
                "write_guard": {
                    "installed": True,
                    "write_attempt_count": 0,
                    "blocked_paths": [],
                },
                "clear_called": True,
                "finalize_attempted": True,
                "finalize_called": True,
                "cleanup_errors": [],
            },
            "projection_evidence": self.projection_evidence,
            "local_schedule_binding": self.schedule_binding,
            "strategy_config": strategy,
            "strategy_config_sha256": strategy_sha256,
            "worker_isolation": isolation,
            "repair_discovery": {
                "schema": "cfdpipe.coarse_repair_discovery.v1",
                "status": "PASS",
                "profile_complete": True,
                "audit_only": True,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "runtime_mesh_tags_hardcoded": False,
                "runtime_tags_are_audit_only": True,
                "repair_application_performed": True,
                "in_memory_mesh_mutation_performed": True,
                "repair_application_scope": "IN_MEMORY_COMPLETE_AUDIT_ONLY",
                "final_smoothing_application_performed": True,
                "owner_ambiguities": [],
                "observed_counts": counts,
                "stable_signature": signature,
                "stable_signature_sha256": (
                    cli_module._canonical_mapping_sha256(signature)
                ),
            },
        }
        if endpoint_requested is not None and not combined_physical:
            manifest["strategy_evidence"] = {
                "post_mesh_repair": {
                    "subdivision": {
                        "surface_schedule_binding": {
                            "schedule_feasibility_endpoint": endpoint,
                            "schedule_feasibility_application": (
                                endpoint_application
                            ),
                        }
                    }
                }
            }
        if direction_family:
            stable_observation = {
                "wall_surface_fingerprint_inventory": self.wall_fingerprints,
                "bad_source_triangles": [],
                "components": [],
            }
            manifest["repair_discovery"] = {
                "schema": "cfdpipe.coarse_component_direction_discovery.v1",
                "status": "PASS",
                "profile_complete": True,
                "direction_endpoint": strategy["owner_free_direction_endpoint"],
                "stable_observation": stable_observation,
                "stable_observation_sha256": (
                    cli_module._canonical_mapping_sha256(stable_observation)
                ),
                "observed_counts": {
                    "projected_3d_element_count": 294_059,
                    "final_nonpositive_element_count": 0,
                    "final_prism_below_threshold_count": 0,
                    "final_core_tetra_below_gamma_count": 0,
                    "final_nonfinite_count": 0,
                },
                "final_quality": {"status": "PASS"},
                "search": {
                    "status": "PASS",
                    "sharp_owner_used": False,
                    "absolute_coordinate_replay": True,
                    "aba_replay_status": "PASS",
                    "monotonicity_assumed": False,
                },
            }
        if physical_replay:
            manifest["repair_discovery"] = {
                "schema": (
                    cli_module.SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA
                    if direction_continuation
                    else cli_module.HOMOTOPY_DISCOVERY_SCHEMA
                    if physical_homotopy
                    else "cfdpipe.coarse_physical_schedule_fixed_direction_"
                    "discovery.v1"
                ),
                "status": manifest_status,
                "profile_complete": manifest_status == "PASS",
                "observed_counts": {
                    "projected_3d_element_count": 294_059,
                },
                "discovery_sha256": "d" * 64,
            }
        return manifest

    def test_parser_registers_explicit_audit_evidence_inputs(self) -> None:
        parser = cli_module.build_parser()
        parsed = parser.parse_args(self.arguments())
        self.assertIs(parsed.handler, cli_module._handle_pipeline_coarse_repair_audit)
        self.assertEqual(self.projection, parsed.projection_manifest)
        self.assertEqual(self.schedule, parsed.local_schedule)
        endpoint = "minimum-growth-existing-direction-repair"
        parsed_endpoint = parser.parse_args(
            self.arguments(schedule_endpoint=endpoint)
        )
        self.assertEqual(endpoint, parsed_endpoint.schedule_feasibility_endpoint)
        pattern_endpoint = "minimum-growth-owner-free-component-pattern-audit"
        parsed_pattern = parser.parse_args(
            self.arguments(schedule_endpoint=pattern_endpoint)
        )
        self.assertEqual(
            pattern_endpoint, parsed_pattern.schedule_feasibility_endpoint
        )
        parsed_replay = parser.parse_args(
            self.arguments(direction_replay=True)
        )
        self.assertEqual(
            self.direction_sources[0][0],
            parsed_replay.direction_audit_primary,
        )
        self.assertEqual(
            self.direction_sources[1][1],
            parsed_replay.direction_audit_confirmation_sha256,
        )
        parsed_homotopy = parser.parse_args(
            self.arguments(
                schedule_endpoint=cli_module.PHYSICAL_HOMOTOPY_REQUEST,
                direction_replay=True,
            )
        )
        self.assertEqual(
            cli_module.PHYSICAL_HOMOTOPY_REQUEST,
            parsed_homotopy.schedule_feasibility_endpoint,
        )
        continuation_arguments = self.arguments(
            schedule_endpoint=cli_module.SCHEDULE_CONTINUATION_REQUEST,
            direction_replay=True,
        )
        output_index = continuation_arguments.index("--output")
        continuation_arguments[output_index:output_index] = [
            "--homotopy-audit-manifest",
            str(self.output_root / "homotopy" / "coarse_repair_audit_manifest.json"),
            "--homotopy-audit-manifest-sha256",
            "f" * 64,
        ]
        parsed_continuation = parser.parse_args(continuation_arguments)
        self.assertEqual(
            cli_module.SCHEDULE_CONTINUATION_REQUEST,
            parsed_continuation.schedule_feasibility_endpoint,
        )
        self.assertEqual(
            "f" * 64, parsed_continuation.homotopy_audit_manifest_sha256
        )

    def test_success_uses_isolated_worker_argv_and_never_requests_shell(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(0, code)
        self.assertEqual(1, len(_AuditRunner.instances))
        _, arguments, kwargs = _AuditRunner.instances[0].calls[0]
        self.assertEqual("cfdpipe.coarse_repair_audit_worker", arguments[1])
        for flag in (
            "--config-sha256",
            "--coarse-contract-sha256",
            "--projection-manifest",
            "--projection-sha256",
            "--local-schedule",
            "--local-schedule-sha256",
        ):
            self.assertIn(flag, arguments)
        self.assertNotIn("shell", kwargs)
        self.assertFalse(list(self.output.glob("*.msh")))
        self.assertFalse(list(self.output.glob("*.su2")))

    def test_minimum_growth_endpoint_argv_and_runtime_application_pass(self) -> None:
        endpoint = "minimum-growth-existing-direction-repair"
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(schedule_endpoint=endpoint)
                )
        self.assertEqual(0, code)
        _, arguments, kwargs = _AuditRunner.instances[0].calls[0]
        flag_index = arguments.index("--schedule-feasibility-endpoint")
        self.assertEqual(endpoint, arguments[flag_index + 1])
        self.assertLess(flag_index, arguments.index("--output"))
        self.assertNotIn("shell", kwargs)

    def test_minimum_growth_accepts_bound_schedule_records(self) -> None:
        self.schedule_binding["surface_schedules"] = {
            fingerprint: {
                "surface_fingerprint_id": fingerprint,
                "layer_count": self.layer_count,
                "cumulative_heights_m": list(values),
            }
            for fingerprint, values in self.surface_schedules.items()
        }
        endpoint = "minimum-growth-existing-direction-repair"
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(schedule_endpoint=endpoint)
                )
        self.assertEqual(0, code)

    def test_pattern_endpoint_argv_and_component_discovery_gate_pass(self) -> None:
        endpoint = "minimum-growth-owner-free-component-pattern-audit"

        def validate_component(discovery, *, expected_direction_endpoint):
            self.assertEqual(
                make_owner_free_direction_pattern_endpoint(
                    schedule_endpoint_sha256=discovery["direction_endpoint"][
                        "schedule_endpoint_sha256"
                    ]
                ),
                expected_direction_endpoint,
            )
            return dict(discovery)

        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner), mock.patch.object(
            cli_module,
            "validate_component_direction_discovery",
            side_effect=validate_component,
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(schedule_endpoint=endpoint)
                )
        self.assertEqual(0, code)
        _, arguments, kwargs = _AuditRunner.instances[0].calls[0]
        flag_index = arguments.index("--schedule-feasibility-endpoint")
        self.assertEqual(endpoint, arguments[flag_index + 1])
        self.assertLess(flag_index, arguments.index("--output"))
        self.assertNotIn("shell", kwargs)

    def test_physical_replay_forwards_two_attestations_and_approval_without_shell(self) -> None:
        with self._replay_patches(), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(0, code)
        self.assertEqual(1, len(_AuditRunner.instances))
        _, arguments, kwargs = _AuditRunner.instances[0].calls[0]
        expected = (
            ("--direction-audit-primary", str(self.direction_sources[0][0].resolve())),
            ("--direction-audit-primary-sha256", self.direction_sources[0][1]),
            (
                "--direction-audit-confirmation",
                str(self.direction_sources[1][0].resolve()),
            ),
            (
                "--direction-audit-confirmation-sha256",
                self.direction_sources[1][1],
            ),
            (
                "--direction-replay-approval-sha256",
                self.replay_approval["approval_sha256"],
            ),
        )
        for flag, value in expected:
            index = arguments.index(flag)
            self.assertEqual(value, arguments[index + 1])
            self.assertLess(index, arguments.index("--output"))
        self.assertIs(kwargs["check"], False)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn(
            "--physical-schedule-homotopy-endpoint-sha256", arguments
        )
        self.assertNotIn("--schedule-feasibility-endpoint", arguments)
        self.assertFalse(list(self.output.glob("*.msh")))
        self.assertFalse(list(self.output.glob("*.su2")))

    def test_physical_homotopy_forwards_attested_endpoint_without_shell(self) -> None:
        endpoint = cli_module.make_physical_schedule_homotopy_endpoint(
            baseline_binding_sha256=self.schedule_binding[
                "binding_sha256"
            ],
            first_layer_height_m=self.first_layer_height_m,
            layer_count=self.layer_count,
        )
        with self._replay_patches(), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(
                        schedule_endpoint=cli_module.PHYSICAL_HOMOTOPY_REQUEST,
                        direction_replay=True,
                    )
                )
        self.assertEqual(0, code)
        self.assertEqual(1, len(_AuditRunner.instances))
        _, arguments, kwargs = _AuditRunner.instances[0].calls[0]
        request_index = arguments.index("--schedule-feasibility-endpoint")
        self.assertEqual(
            cli_module.PHYSICAL_HOMOTOPY_REQUEST,
            arguments[request_index + 1],
        )
        sha_index = arguments.index(
            "--physical-schedule-homotopy-endpoint-sha256"
        )
        self.assertEqual(endpoint["endpoint_sha256"], arguments[sha_index + 1])
        self.assertLess(request_index, sha_index)
        self.assertLess(sha_index, arguments.index("--output"))
        self.assertIs(kwargs["check"], False)
        self.assertNotIn("shell", kwargs)
        manifest = json.loads(
            (self.output / "coarse_repair_audit_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            cli_module.PHYSICAL_HOMOTOPY_PURPOSE,
            manifest["audit_purpose"],
        )
        self.assertEqual(
            endpoint, manifest["physical_schedule_homotopy_endpoint"]
        )
        self.assertEqual(
            endpoint,
            manifest["worker_isolation"]["direction_replay"][
                "homotopy_endpoint"
            ],
        )
        self.assertEqual(
            endpoint,
            manifest["strategy_config"]["schedule_feasibility_endpoint"],
        )
        self.assertEqual(
            endpoint,
            manifest["strategy_config"][
                "physical_schedule_homotopy_endpoint"
            ],
        )

    def test_physical_homotopy_requires_two_direction_attestations(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(
                        schedule_endpoint=cli_module.PHYSICAL_HOMOTOPY_REQUEST
                    )
                )
        self.assertEqual(2, code)
        self.assertEqual([], _AuditRunner.instances)

    def test_first_frontier_direction_requires_explicit_homotopy_attestation(self):
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(
                        schedule_endpoint=(
                            cli_module.SCHEDULE_CONTINUATION_REQUEST
                        ),
                        direction_replay=True,
                    )
                )
        self.assertEqual(2, code)
        self.assertEqual([], _AuditRunner.instances)

    def test_first_frontier_direction_forwards_all_attested_inputs_without_shell(self):
        homotopy_source = (
            self.output_root
            / "physical-homotopy-source"
            / "coarse_repair_audit_manifest.json"
        )
        homotopy_source.parent.mkdir()
        homotopy_source.write_text(
            '{"status":"INCOMPLETE"}\n', encoding="utf-8"
        )
        homotopy_sha256 = hashlib.sha256(
            homotopy_source.read_bytes()
        ).hexdigest()
        self.homotopy_audit_evidence = {
            "path": str(homotopy_source.resolve()),
            "sha256": homotopy_sha256,
            "size_bytes": homotopy_source.stat().st_size,
            "manifest_status": "INCOMPLETE",
            "discovery": {"validated": True},
        }
        self.schedule_direction_continuation_endpoint = {
            "schema": "test.frontier-direction-endpoint.v1",
            "endpoint_sha256": "6" * 64,
        }
        arguments = self.arguments(
            schedule_endpoint=cli_module.SCHEDULE_CONTINUATION_REQUEST,
            direction_replay=True,
        )
        output_index = arguments.index("--output")
        arguments[output_index:output_index] = [
            "--homotopy-audit-manifest",
            str(homotopy_source),
            "--homotopy-audit-manifest-sha256",
            homotopy_sha256,
        ]
        _AuditRunner.returncode = 2
        with self._replay_patches(), mock.patch.object(
            cli_module,
            "load_and_validate_physical_homotopy_audit_manifest",
            return_value=copy.deepcopy(self.homotopy_audit_evidence),
        ), mock.patch.object(
            cli_module,
            "make_schedule_frontier_direction_endpoint",
            return_value=copy.deepcopy(
                self.schedule_direction_continuation_endpoint
            ),
        ), mock.patch.object(
            cli_module,
            "validate_schedule_frontier_direction_endpoint",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ), mock.patch.object(
            cli_module,
            "validate_schedule_frontier_direction_continuation",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ), mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(arguments)

        self.assertEqual(2, code)
        self.assertEqual(1, len(_AuditRunner.instances))
        _, forwarded, kwargs = _AuditRunner.instances[0].calls[0]
        expected_pairs = (
            (
                "--schedule-feasibility-endpoint",
                cli_module.SCHEDULE_CONTINUATION_REQUEST,
            ),
            ("--homotopy-audit-manifest", str(homotopy_source.resolve())),
            ("--homotopy-audit-manifest-sha256", homotopy_sha256),
            (
                "--schedule-direction-continuation-endpoint-sha256",
                self.schedule_direction_continuation_endpoint[
                    "endpoint_sha256"
                ],
            ),
        )
        for flag, expected in expected_pairs:
            index = forwarded.index(flag)
            self.assertEqual(expected, forwarded[index + 1])
            self.assertLess(index, forwarded.index("--output"))
        self.assertIs(kwargs["check"], False)
        self.assertNotIn("shell", kwargs)
        manifest = json.loads(
            (self.output / "coarse_repair_audit_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            cli_module.SCHEDULE_CONTINUATION_PURPOSE,
            manifest["audit_purpose"],
        )
        self.assertEqual(
            self.schedule_direction_continuation_endpoint,
            manifest["worker_isolation"]["direction_replay"][
                "schedule_direction_continuation_endpoint"
            ],
        )

    def test_legal_physical_homotopy_incomplete_returns_two(self) -> None:
        _AuditRunner.returncode = 2
        validated = mock.Mock(
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value))
        )
        with self._replay_patches(
            homotopy_validator=validated
        ), mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(
                        schedule_endpoint=cli_module.PHYSICAL_HOMOTOPY_REQUEST,
                        direction_replay=True,
                    )
                )
        self.assertEqual(2, code)
        validated.assert_called_once()
        expected_endpoint = validated.call_args.kwargs["expected_endpoint"]
        self.assertEqual(
            expected_endpoint["endpoint_sha256"],
            cli_module.make_physical_schedule_homotopy_endpoint(
                baseline_binding_sha256=self.schedule_binding[
                    "binding_sha256"
                ],
                first_layer_height_m=self.first_layer_height_m,
                layer_count=self.layer_count,
            )["endpoint_sha256"],
        )
        manifest = json.loads(
            (self.output / "coarse_repair_audit_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("INCOMPLETE", manifest["status"])
        self.assertEqual("PASS", manifest["worker_isolation"]["status"])
        self.assertEqual(
            "INCOMPLETE",
            manifest["worker_isolation"]["audit_result_status"],
        )

    def test_physical_replay_validator_receives_numeric_schedule_projection(self) -> None:
        captured = {}

        def validate_physical(discovery, **kwargs):
            schedules = kwargs["expected_surface_schedules"]
            captured["schedules"] = copy.deepcopy(schedules)
            self.assertEqual(self.surface_schedules, schedules)
            self.assertEqual(self.wall_fingerprints, sorted(schedules))
            self.assertTrue(
                all(isinstance(values, list) for values in schedules.values())
            )
            self.assertTrue(
                all(
                    isinstance(value, float)
                    for values in schedules.values()
                    for value in values
                )
            )
            self.assertTrue(
                all(
                    len(values) == self.layer_count
                    for values in schedules.values()
                )
            )
            return copy.deepcopy(dict(discovery))

        with self._replay_patches(
            physical_validator=validate_physical
        ), mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(0, code)
        self.assertEqual(self.surface_schedules, captured["schedules"])

    def test_physical_replay_consensus_failure_stops_before_worker(self) -> None:
        error = cli_module.CoarseDirectionReplayError(
            "independent direction sources disagree"
        )
        with self._replay_patches(consensus_error=error), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(2, code)
        self.assertEqual([], _AuditRunner.instances)
        report = (
            self.output_root
            / "_worker_evidence"
            / self.output.name
            / "repair_audit_preflight.json"
        )
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            "DIRECTION_REPLAY_CONSENSUS_INVALID",
            saved["reasons"][0]["code"],
        )

    def test_physical_replay_approval_mismatch_after_worker_is_rejected(self) -> None:
        def mismatched_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            manifest["direction_replay_approval"] = {
                **manifest["direction_replay_approval"],
                "approval_sha256": "e" * 64,
            }
            return manifest

        _AuditRunner.manifest_factory = mismatched_manifest
        with self._replay_patches(), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(2, code)

    def test_physical_replay_source_tamper_after_worker_is_rejected(self) -> None:
        def tampered_source_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            self.direction_sources[0][0].write_text(
                '{"tampered":true}\n', encoding="utf-8"
            )
            return manifest

        _AuditRunner.manifest_factory = tampered_source_manifest
        with self._replay_patches(), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(2, code)

    def test_legal_physical_incomplete_is_strictly_validated_and_returns_two(self) -> None:
        _AuditRunner.returncode = 2
        validated = mock.Mock(
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value))
        )
        with self._replay_patches(physical_validator=validated), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(2, code)
        validated.assert_called_once()
        discovery = validated.call_args.args[0]
        self.assertEqual("INCOMPLETE", discovery["status"])
        manifest = json.loads(
            (self.output / "coarse_repair_audit_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("INCOMPLETE", manifest["status"])
        self.assertEqual("PASS", manifest["worker_isolation"]["status"])
        self.assertEqual(
            "INCOMPLETE",
            manifest["worker_isolation"]["audit_result_status"],
        )
        _, _, kwargs = _AuditRunner.instances[0].calls[0]
        self.assertIs(kwargs["check"], False)

    def test_physical_replay_nonzero_infrastructure_result_is_not_incomplete(self) -> None:
        _AuditRunner.returncode = 1
        validated = mock.Mock(
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value))
        )
        with self._replay_patches(physical_validator=validated), mock.patch.object(
            cli_module, "CommandRunner", _AuditRunner
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(
                    self.arguments(direction_replay=True)
                )
        self.assertEqual(1, code)
        validated.assert_not_called()

    def test_resource_abort_publishes_strict_failure_evidence_before_return(self) -> None:
        class ResourceAbortRunner:
            def __init__(self, output_dir, **kwargs) -> None:
                self.output_dir = Path(output_dir)

            def run(self, executable, args=(), **kwargs):
                result = SimpleNamespace(
                    executable=str(executable),
                    returncode=3221225786,
                    stderr="original resource stderr",
                    resource_aborted=True,
                )
                raise cli_module.CommandExecutionError(
                    result, "resource guard stopped worker"
                )

        publisher = mock.Mock(return_value={"status": "FAIL"})
        stderr = io.StringIO()
        with mock.patch.object(
            cli_module, "CommandRunner", ResourceAbortRunner
        ), mock.patch.object(
            cli_module,
            "publish_coarse_repair_resource_abort_manifest",
            publisher,
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = cli_module.main(self.arguments())

        self.assertEqual(1, code)
        evidence = self.output_root / "_worker_evidence" / self.output.name
        publisher.assert_called_once_with(
            evidence_directory=evidence,
            command_metadata_path=evidence / "worker.command.json",
            source_brep_path=self.source.resolve(),
            expected_source_sha256=self.source_sha256,
            output_directory=self.output,
        )
        self.assertFalse(self.output.exists())
        self.assertIn("resource_abort_manifest.json", stderr.getvalue())
        self.assertIn("original resource stderr", stderr.getvalue())

    def test_endpoint_missing_or_tampered_application_is_rejected(self) -> None:
        endpoint = "minimum-growth-existing-direction-repair"
        for mode in ("missing", "tampered"):
            with self.subTest(mode=mode):
                self.output = self.output_root / f"endpoint-{mode}"
                _AuditRunner.instances.clear()
                def forged_manifest(output, arguments, *, selected=mode):
                    manifest = self._manifest(output, arguments)
                    binding = manifest["strategy_evidence"][
                        "post_mesh_repair"
                    ]["subdivision"]["surface_schedule_binding"]
                    if selected == "missing":
                        binding.pop("schedule_feasibility_application")
                    else:
                        binding["schedule_feasibility_application"][
                            "candidate_growth_ratio"
                        ] = 1.01
                    return manifest

                _AuditRunner.manifest_factory = forged_manifest
                with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
                    with redirect_stdout(io.StringIO()), redirect_stderr(
                        io.StringIO()
                    ):
                        code = cli_module.main(
                            self.arguments(schedule_endpoint=endpoint)
                        )
                self.assertEqual(2, code)

    def test_ordinary_audit_rejects_explicit_null_endpoint_fields(self) -> None:
        def forged_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            strategy = manifest["strategy_config"]
            strategy["schedule_feasibility_endpoint"] = None
            strategy_without_hash = dict(strategy)
            strategy_without_hash.pop("normalized_config_sha256")
            strategy_hash = cli_module._canonical_mapping_sha256(
                strategy_without_hash
            )
            strategy["normalized_config_sha256"] = strategy_hash
            manifest["strategy_config_sha256"] = strategy_hash
            return manifest

        _AuditRunner.manifest_factory = forged_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_second_point_is_rejected_before_runner(self) -> None:
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments(characteristic="0.32"))
        self.assertEqual(2, code)
        self.assertEqual([], _AuditRunner.instances)
        report = (
            self.output_root
            / "_worker_evidence"
            / self.output.name
            / "repair_audit_preflight.json"
        )
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual("FIRST_POINT_REQUIRED", saved["reasons"][0]["code"])
        self.assertFalse(saved["policy"]["command_runner_constructed"])

    def test_zero_return_with_incomplete_manifest_is_rejected(self) -> None:
        def weak_manifest(output, arguments):
            return {
                "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
                "status": "PASS",
                "audit_only": True,
            }

        _AuditRunner.manifest_factory = weak_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)
        self.assertTrue(
            (self.output / "coarse_repair_audit_manifest.json").is_file()
        )

    def test_self_hashed_simplified_discovery_is_rejected(self) -> None:
        def simplified_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            discovery = manifest["repair_discovery"]
            discovery.pop("runtime_tags_are_audit_only")
            return manifest

        _AuditRunner.manifest_factory = simplified_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_zero_return_with_forged_worker_isolation_is_rejected(self) -> None:
        def forged_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            manifest["worker_isolation"]["worker_memory"]["hard_limit"][
                "requested_process_memory_limit_bytes"
            ] = 1
            return manifest

        _AuditRunner.manifest_factory = forged_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_production_memory_schema_and_single_pid_are_required(self) -> None:
        def forged_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            memory = manifest["worker_isolation"]["worker_memory"]
            memory["system_before_limit"]["schema"] = "test.memory"
            memory["process_after_audit"]["process_id"] = 9999
            self._persist_isolation(output, manifest)
            return manifest

        _AuditRunner.manifest_factory = forged_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_noncanonical_worker_utc_offset_is_rejected(self) -> None:
        def offset_manifest(output, arguments):
            manifest = self._manifest(output, arguments)
            isolation = manifest["worker_isolation"]
            isolation["started_at_utc"] = "2026-08-02T08:00:00+08:00"
            isolation["ended_at_utc"] = "2026-08-02T08:00:01+08:00"
            self._persist_isolation(output, manifest)
            return manifest

        _AuditRunner.manifest_factory = offset_manifest
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_duplicate_manifest_json_key_is_rejected(self) -> None:
        _AuditRunner.manifest_factory = lambda output, arguments: (
            '{"schema":"cfdpipe.coarse_repair_audit_manifest.v1",'
            '"status":"PASS","status":"PASS"}'
        )
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_duplicate_persisted_worker_json_key_is_rejected(self) -> None:
        def duplicate_evidence(output, arguments):
            manifest = self._manifest(output, arguments)
            path = (
                output.parent
                / "_worker_evidence"
                / output.name
                / "repair_audit_worker.json"
            )
            encoded = json.dumps(manifest["worker_isolation"], allow_nan=False)
            encoded = encoded.replace(
                '"status": "PASS"',
                '"status": "PASS", "status": "PASS"',
                1,
            )
            path.write_text(encoded, encoding="utf-8")
            return manifest

        _AuditRunner.manifest_factory = duplicate_evidence
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_config_is_rehashed_after_worker_exit(self) -> None:
        def stale_config(output, arguments):
            manifest = self._manifest(output, arguments)
            self.config.write_text('schema = "changed"\n', encoding="utf-8")
            return manifest

        _AuditRunner.manifest_factory = stale_config
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_projection_is_rehashed_after_worker_exit(self) -> None:
        def stale_projection(output, arguments):
            manifest = self._manifest(output, arguments)
            self.projection.write_text('{"changed":true}\n', encoding="utf-8")
            return manifest

        _AuditRunner.manifest_factory = stale_projection
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)

    def test_source_brep_is_rehashed_after_worker_exit(self) -> None:
        def stale_source(output, arguments):
            manifest = self._manifest(output, arguments)
            self.source.chmod(stat.S_IREAD | stat.S_IWRITE)
            self.source.write_text("changed BREP source\n", encoding="utf-8")
            self.source.chmod(stat.S_IREAD)
            return manifest

        _AuditRunner.manifest_factory = stale_source
        with mock.patch.object(cli_module, "CommandRunner", _AuditRunner):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli_module.main(self.arguments())
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
