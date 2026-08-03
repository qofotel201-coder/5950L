from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cfdpipe.coarse_direction_replay as replay
from cfdpipe.coarse_direction_feasibility import (
    make_owner_free_direction_pattern_endpoint,
)
from cfdpipe.coarse_schedule_feasibility import make_minimum_growth_endpoint


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical_sha256(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _quality(*, minimum_prism: float = 0.02) -> dict:
    unsigned = {
        "status": "PASS",
        "minimum_prism_scaled_jacobian_for_pass": 0.01,
        "minimum_core_tetra_gamma_for_pass": 0.001,
        "all_values_finite": True,
        "nonfinite_count": 0,
        "nonpositive_element_count": 0,
        "prism_below_threshold_element_count": 0,
        "core_tetra_below_gamma_count": 0,
        "minimum_prism_scaled_jacobian": minimum_prism,
        "minimum_core_tetra_gamma": 0.01,
        "prism_element_count": 60,
        "core_element_count": 10,
        "core_tetra_count": 10,
        "maximum_prism_scaled_jacobian_deficit": 0.0,
        "prism_scaled_jacobian_deficit_l1": 0.0,
        "prism_scaled_jacobian_deficit_l2": 0.0,
        "maximum_core_tetra_gamma_deficit": 0.0,
        "core_tetra_gamma_deficit_l2": 0.0,
    }
    return {**unsigned, "quality_sha256": _canonical_sha256(unsigned)}


def _discovery() -> dict:
    roots = [_sha(f"root-{index}") for index in range(3)]
    triangle = _sha("bad-triangle")
    wall = _sha("wall")
    base_component = _sha("base-component")
    interaction_component = _sha("interaction-component")
    final_quality = _quality()
    stable_observation = {
        "wall_surface_fingerprint_inventory": [wall],
        "bad_source_triangles": [
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": wall,
                "root_coordinate_sha256": roots,
            }
        ],
        "components": [
            {
                "component_sha256": base_component,
                "connected_by_shared_root": True,
                "root_coordinate_sha256": roots,
                "root_count": len(roots),
                "sharp_owner_used": False,
                "source_triangle_sha256": [triangle],
                "split_by_wall_fingerprint": False,
                "triangle_count": 1,
                "wall_surface_fingerprints": [wall],
            }
        ],
    }
    topology_unsigned = {
        "schema": "cfdpipe.owner_free_interaction_topology.v1",
        "status": "PASS",
        "base_component_count": 1,
        "interaction_component_count": 1,
        "candidate_root_count": len(roots),
        "candidate_chain_node_count": 183,
        "affected_prism_count": 60,
        "affected_core_element_count": 10,
        "affected_core_tetra_count": 10,
        "cross_base_component_prism_count": 0,
        "cross_base_component_core_count": 0,
        "base_components_merged": False,
        "components": [
            {
                "interaction_component_sha256": interaction_component,
                "root_coordinate_sha256": roots,
                "candidate_root_count": len(roots),
                "source_component_sha256": [base_component],
                "source_component_count": 1,
                "affected_prism_count": 60,
                "affected_core_element_count": 10,
                "affected_core_tetra_count": 10,
                "affected_element_lineage_sha256": _sha("affected-elements"),
                "connected_by_affected_element_cooccurrence": True,
                "sharp_owner_used": False,
                "split_by_wall_fingerprint": False,
            }
        ],
    }
    topology = {
        **topology_unsigned,
        "interaction_topology_sha256": _canonical_sha256(topology_unsigned),
    }
    component_recheck_unsigned = {
        "schema": "cfdpipe.owner_free_final_component_recheck.v1",
        "status": "PASS",
        "component_count": 1,
        "quality_evaluation_count": 1,
        "all_records_final_state": True,
        "component_records": [],
    }
    component_recheck = {
        **component_recheck_unsigned,
        "final_component_recheck_sha256": _canonical_sha256(
            component_recheck_unsigned
        ),
    }
    low_quality_unsigned = {
        "schema": "cfdpipe.owner_free_low_quality_prism_inventory.v1",
        "status": "PASS",
        "minimum_scaled_jacobian_for_pass": 0.01,
        "final_low_quality_prism_count": 0,
        "lineage_quality_query_count": 1,
        "records": [],
    }
    low_quality = {
        **low_quality_unsigned,
        "final_low_quality_prism_inventory_sha256": _canonical_sha256(
            low_quality_unsigned
        ),
    }
    coordinate_replay = {
        "encoding": "python-float-hex/root-sha/layer-v1",
        "candidate_root_count": len(roots),
        "candidate_chain_node_count": 183,
        "state_a_first_coordinate_sha256": _sha("state-a-coordinates"),
        "state_b_first_coordinate_sha256": _sha("state-b-coordinates"),
        "state_a_second_coordinate_sha256": _sha("state-a-coordinates"),
        "state_b_restored_coordinate_sha256": _sha("state-b-coordinates"),
        "state_a_first_quality_sha256": _sha("state-a-quality"),
        "state_b_first_quality_sha256": final_quality["quality_sha256"],
        "state_a_second_quality_sha256": _sha("state-a-quality"),
        "state_b_restored_quality_sha256": final_quality["quality_sha256"],
        "state_a_reproducible": True,
        "state_b_reproducible": True,
        "a_b_distinct": True,
    }
    return {
        "schema": "cfdpipe.coarse_component_direction_discovery.v1",
        "status": "PASS",
        "stable_observation": stable_observation,
        "stable_observation_sha256": _canonical_sha256(stable_observation),
        "final_quality": final_quality,
        "search": {
            "schema": "cfdpipe.coarse_component_direction_search.v1",
            "status": "PASS",
            "candidate_root_count": len(roots),
            "changed_roots": [
                {
                    "root_coordinate_sha256": roots[0],
                    "original_direction": [1.0, 0.0, 0.0],
                    "selected_direction": [0.0, 1.0, 0.0],
                    "selected_source": "component_chebyshev",
                    "selected_interior_weight": 0.5,
                    "candidate_count": 7,
                }
            ],
            "coordinate_replay": coordinate_replay,
            "interaction_topology": topology,
            "final_component_recheck": component_recheck,
            "final_low_quality_prisms": low_quality,
        },
    }


class CoarseDirectionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.contract_sha = _sha("coarse-contract")
        self.worker_limit = 3 * 1024 * 1024 * 1024
        schedule_endpoint = make_minimum_growth_endpoint(
            baseline_binding_sha256=_sha("local-schedule-binding"),
            first_layer_height_m=1.0e-6,
            layer_count=60,
        )
        direction_endpoint = make_owner_free_direction_pattern_endpoint(
            schedule_endpoint_sha256=schedule_endpoint["endpoint_sha256"]
        )
        self.strategy = {
            "normalized_config_sha256": _sha("strategy"),
            "schedule_feasibility_endpoint": schedule_endpoint,
            "owner_free_direction_endpoint": direction_endpoint,
        }
        self.local_schedule = {
            "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
            "status": "PASS",
            "binding_sha256": _sha("local-schedule-binding"),
            "source_plan_path": str(
                self.root / "schedule" / "boundary_layer_local_schedule.json"
            ),
            "source_plan_sha256": _sha("local-schedule-plan"),
        }
        self.projection = {
            "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
            "status": "PASS",
            "sha256": _sha("projection-manifest"),
            "path": str(self.root / "projection" / "projection_manifest.json"),
            "projected_3d_elements": 70,
        }
        self.expected = {
            "expected_audit_strategy": self.strategy,
            "expected_coarse_contract_sha256": self.contract_sha,
            "expected_characteristic_length_m": 0.4,
            "expected_local_schedule_binding": self.local_schedule,
            "expected_projection_evidence": self.projection,
            "expected_projected_3d_elements": 70,
            "expected_worker_memory_limit_bytes": self.worker_limit,
            "expected_minimum_prism_scaled_jacobian": 0.01,
            "expected_minimum_core_tetra_gamma": 0.001,
        }
        self.validation_expected = {
            "expected_coarse_contract_sha256": self.contract_sha,
            "expected_characteristic_length_m": 0.4,
            "expected_local_schedule_binding_sha256": self.local_schedule[
                "binding_sha256"
            ],
            "expected_projection_manifest_sha256": self.projection["sha256"],
            "expected_projected_3d_elements": 70,
            "expected_minimum_prism_scaled_jacobian": 0.01,
            "expected_minimum_core_tetra_gamma": 0.001,
        }
        self._pair_index = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self, *, process_id: int, ordinal: int) -> dict:
        source = {
            "path": str(self.root / "geometry" / "source.brep"),
            "sha256": _sha("source-brep"),
            "size_bytes": 123,
            "read_only": True,
        }
        started = f"2026-08-02T00:0{ordinal}:00.000000Z"
        ended = f"2026-08-02T00:0{ordinal}:30.000000Z"
        discovery = _discovery()
        discovery["direction_endpoint"] = copy.deepcopy(
            self.strategy["owner_free_direction_endpoint"]
        )
        discovery["observed_counts"] = {
            "candidate_root_count": 3,
            "changed_root_count": 1,
            "projected_3d_element_count": 70,
        }
        discovery["subdivision"] = {
            "schedule_endpoint_sha256": self.strategy[
                "schedule_feasibility_endpoint"
            ]["endpoint_sha256"],
            "surface_schedule_binding_sha256": self.local_schedule[
                "binding_sha256"
            ],
        }
        discovery["search"]["aba_replay"] = {
            "state_a_first_sha256": _sha("state-a-quality"),
            "state_b_sha256": discovery["final_quality"]["quality_sha256"],
            "state_a_second_sha256": _sha("state-a-quality"),
            "state_a_reproducible": True,
        }
        return {
            "schema": replay.AUDIT_MANIFEST_SCHEMA,
            "status": "PASS",
            "audit_only": True,
            "audit_purpose": replay.PATTERN_PURPOSE,
            "schedule_feasibility_endpoint_requested": replay.PATTERN_REQUEST,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "external_commands": [],
            "source_before": source,
            "source_after": copy.deepcopy(source),
            "source_unchanged": True,
            "started_at_utc": started,
            "ended_at_utc": ended,
            "gmsh_session": {
                "initialize_called": True,
                "finalize_called": True,
                "cleanup_errors": [],
                "logger_capture_succeeded": True,
                "diagnostic_audit": {"status": "PASS"},
            },
            "worker_isolation": {
                "schema": "cfdpipe.coarse_repair_audit_worker.v1",
                "status": "PASS",
                "audit_result_status": "PASS",
                "audit_only": True,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "su2_called": False,
                "paraview_called": False,
                "coarse_contract_sha256": self.contract_sha,
                "config_sha256": _sha("config-file"),
                "worker": {
                    "argv": [],
                    "audit_runner_called": True,
                    "characteristic_length_m": 0.4,
                    "config_path": str(self.root / "config" / "coarse_mesh.toml"),
                    "configured_process_memory_limit_bytes": self.worker_limit,
                    "evidence_path": "PENDING",
                    "hard_limit_installed_before_heavy_import": True,
                    "heavy_dependencies_imported": True,
                    "local_schedule_bound_after_heavy_import": True,
                    "manifest_path": "PENDING",
                    "normalized_resource_matches_raw": True,
                    "output_directory": "PENDING",
                },
                "worker_memory": {
                    "hard_limit": {
                        "schema": "cfdpipe.worker_memory_limit.v1",
                        "status": "PASS",
                        "supported": True,
                        "platform": "win32",
                        "backend": "windows_job_object",
                        "operation": "install_current_process_memory_limit",
                        "error": None,
                        "process_id": process_id,
                        "requested_process_memory_limit_bytes": self.worker_limit,
                        "job_object": {
                            "process_memory_limit_bytes": self.worker_limit,
                            "process_assigned": True,
                            "handle_retained_for_process_lifetime": True,
                            "job_handle_value": process_id + 10,
                            "limit_flags": 0x2100,
                        },
                    },
                    "process_after_limit": {
                        "schema": "cfdpipe.worker_process_memory.v2",
                        "status": "PASS",
                        "supported": True,
                        "process_id": process_id,
                    },
                    "process_after_audit": {
                        "schema": "cfdpipe.worker_process_memory.v2",
                        "process_id": process_id,
                        "status": "PASS",
                        "supported": True,
                    }
                },
            },
            "run_evidence": {
                "coarse_contract_sha256": self.contract_sha,
                "config_path": str(self.root / "config" / "coarse_mesh.toml"),
                "config_sha256": _sha("config-file"),
                "schedule_feasibility_endpoint": replay.PATTERN_REQUEST,
                "worker_argv": [],
            },
            "characteristic_length_m": 0.4,
            "coarse_contract_sha256": self.contract_sha,
            "strategy_config": copy.deepcopy(self.strategy),
            "strategy_config_sha256": self.strategy[
                "normalized_config_sha256"
            ],
            "local_schedule_binding": copy.deepcopy(self.local_schedule),
            "projection_evidence": copy.deepcopy(self.projection),
            "repair_discovery": discovery,
        }

    def _write_manifest(self, directory: str, manifest: dict) -> tuple[Path, str]:
        path = self.root / directory / "coarse_repair_audit_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=False)
        worker = manifest["worker_isolation"]["worker"]
        worker["manifest_path"] = str(path)
        worker["output_directory"] = str(path.parent)
        worker["evidence_path"] = str(
            self.root / "worker-evidence" / directory / "repair_audit_worker.json"
        )
        argv = [
            str(self.root / "venv" / "python.exe"),
            "-m",
            "cfdpipe.coarse_repair_audit_worker",
            "--config",
            worker["config_path"],
            "--config-sha256",
            manifest["worker_isolation"]["config_sha256"],
            "--coarse-contract-sha256",
            self.contract_sha,
            "--characteristic-length",
            "0.40000000000000002",
            "--projection-manifest",
            self.projection["path"],
            "--projection-sha256",
            self.projection["sha256"],
            "--local-schedule",
            self.local_schedule["source_plan_path"],
            "--local-schedule-sha256",
            self.local_schedule["source_plan_sha256"],
            "--schedule-feasibility-endpoint",
            replay.PATTERN_REQUEST,
            "--output",
            str(path.parent),
        ]
        worker["argv"] = argv
        manifest["run_evidence"]["worker_argv"] = copy.deepcopy(argv)
        path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_pair(
        self,
        *,
        second_mutation=None,
    ) -> tuple[Path, str, Path, str]:
        self._pair_index += 1
        prefix = f"pair-{self._pair_index}"
        first = self._manifest(process_id=1001, ordinal=1)
        second = self._manifest(process_id=1002, ordinal=2)
        if second_mutation is not None:
            second_mutation(second)
        first_path, first_sha = self._write_manifest(f"{prefix}-first", first)
        second_path, second_sha = self._write_manifest(f"{prefix}-second", second)
        return first_path, first_sha, second_path, second_sha

    def _load(self, pair):
        first_path, first_sha, second_path, second_sha = pair
        with mock.patch(
            "cfdpipe.coarse_direction_replay.validate_component_direction_discovery",
            side_effect=lambda value, **_kwargs: copy.deepcopy(value),
        ):
            return replay.load_direction_replay_consensus_approval(
                first_path,
                first_sha,
                second_path,
                second_sha,
                **self.expected,
            )

    @staticmethod
    def _rehash_approval(approval: dict) -> None:
        unsigned = copy.deepcopy(approval)
        unsigned.pop("approval_sha256", None)
        approval["approval_sha256"] = _canonical_sha256(unsigned)

    @staticmethod
    def _rehash_quality(quality: dict) -> None:
        unsigned = copy.deepcopy(quality)
        unsigned.pop("quality_sha256", None)
        quality["quality_sha256"] = _canonical_sha256(unsigned)

    @staticmethod
    def _rehash_discovery(discovery: dict) -> None:
        unsigned = copy.deepcopy(discovery)
        unsigned.pop("discovery_sha256", None)
        discovery["discovery_sha256"] = _canonical_sha256(unsigned)

    def _physical_fixture(self, approval: dict) -> tuple[dict, dict]:
        candidate_count = approval["candidate_root_count"]
        changed_count = approval["changed_root_count"]
        unchanged_count = candidate_count - changed_count
        wall = approval["root_contexts"][0][
            "incident_wall_surface_fingerprints"
        ][0]
        surface_schedules = {wall: [0.1, 0.2, 0.3]}
        assignments = []
        for context in approval["root_contexts"]:
            cumulative = surface_schedules[wall]
            assignments.append(
                {
                    "root_coordinate_sha256": context[
                        "root_coordinate_sha256"
                    ],
                    "incident_wall_surface_fingerprints": [wall],
                    "cumulative_heights_sha256": _canonical_sha256(
                        {
                            "cumulative_heights_float_hex": [
                                value.hex() for value in cumulative
                            ]
                        }
                    ),
                    "selected_total_thickness_float_hex": cumulative[-1].hex(),
                }
            )
        assignments.sort(key=lambda record: record["root_coordinate_sha256"])
        changed_roots = [
            {
                "root_coordinate_sha256": record["root_coordinate_sha256"],
                "root_context_sha256": record["root_context_sha256"],
                "original_direction_float_hex": record[
                    "original_direction_float_hex"
                ],
                "selected_direction_float_hex": record[
                    "selected_direction_float_hex"
                ],
                "application": (
                    "ABSOLUTE_ORIGIN_PLUS_DIRECTION_TIMES_REAL_SCHEDULE"
                ),
            }
            for record in approval["changed_roots"]
        ]
        compatibility_records = []
        for record in approval["changed_roots"]:
            original_hex = list(record["original_direction_float_hex"])
            original = [float.fromhex(value) for value in original_hex]
            compatibility_records.append(
                {
                    "root_coordinate_sha256": record[
                        "root_coordinate_sha256"
                    ],
                    "runtime_original_direction_float_hex": original_hex,
                    "consensus_original_direction_float_hex": original_hex,
                    "maximum_absolute_difference_float_hex": float(0.0).hex(),
                    "l2_difference_float_hex": float(0.0).hex(),
                    "dot_product_float_hex": math.fsum(
                        value * value for value in original
                    ).hex(),
                    "matched_exactly": True,
                    "within_absolute_tolerance": True,
                }
            )
        compatibility_records.sort(
            key=lambda record: record["root_coordinate_sha256"]
        )
        compatibility_unsigned = {
            "schema": "cfdpipe.coarse_direction_roundoff_compatibility.v1",
            "status": "PASS",
            "absolute_tolerance_float_hex": math.ulp(1.0).hex(),
            "state_a_uses_fresh_runtime_direction": True,
            "selected_state_uses_consensus_direction": True,
            "changed_root_count": changed_count,
            "exact_match_root_count": changed_count,
            "roundoff_match_root_count": 0,
            "records": compatibility_records,
            "records_sha256": _canonical_sha256(
                {"records": compatibility_records}
            ),
        }
        compatibility = {
            **compatibility_unsigned,
            "compatibility_sha256": _canonical_sha256(
                compatibility_unsigned
            ),
        }
        quality = _quality()
        component_id = approval["root_contexts"][0][
            "interaction_component_sha256"
        ]
        discovery = {
            "schema": replay.PHYSICAL_DISCOVERY_SCHEMA,
            "status": "PASS",
            "profile_complete": True,
            "audit_only": True,
            "audit_variant": "physical_local_schedule_fixed_direction_replay",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "runtime_mesh_tags_hardcoded": False,
            "source_bindings": {
                "direction_replay_approval_sha256": approval["approval_sha256"],
                "source_audit_manifest_sha256": [
                    source["sha256"]
                    for source in approval["source_audit_manifests"]
                ],
                "local_schedule_binding_sha256": approval[
                    "local_schedule_binding_sha256"
                ],
            },
            "fixed_direction_contract": {
                "candidate_root_count": candidate_count,
                "changed_root_count": changed_count,
                "unchanged_root_count": unchanged_count,
                "direction_search_performed": False,
                "absolute_coordinate_replay": True,
                "original_direction_compatibility": compatibility,
                "changed_roots": changed_roots,
            },
            "root_context_replay": {
                "expected_count": candidate_count,
                "matched_count": candidate_count,
                "missing_count": 0,
                "duplicate_count": 0,
                "unexplained_count": 0,
                "contexts": copy.deepcopy(approval["root_contexts"]),
            },
            "schedule_application": {
                "minimum_growth_schedule_used": False,
                "surface_count": 1,
                "layer_count": 3,
                "baseline_surface_schedules_sha256": _canonical_sha256(
                    {"surface_schedules": surface_schedules}
                ),
                "minimum_surface_total_thickness_m": 0.3,
                "maximum_surface_total_thickness_m": 0.3,
                "root_schedule_assignment_count": candidate_count,
                "root_schedule_assignments": assignments,
                "root_schedule_assignments_sha256": _canonical_sha256(
                    {"root_schedule_assignments": assignments}
                ),
            },
            "coordinate_replay": {
                "encoding": "python-float-hex/root-sha/physical-layer-v1",
                "state_a_first_coordinate_sha256": _sha("physical-a"),
                "state_b_first_coordinate_sha256": _sha("physical-b"),
                "state_a_second_coordinate_sha256": _sha("physical-a"),
                "state_b_second_coordinate_sha256": _sha("physical-b"),
                "state_a_first_quality_sha256": _sha("physical-quality-a"),
                "state_b_first_quality_sha256": quality["quality_sha256"],
                "state_a_second_quality_sha256": _sha("physical-quality-a"),
                "state_b_second_quality_sha256": quality["quality_sha256"],
                "state_a_reproducible": True,
                "state_b_reproducible": True,
                "a_b_distinct": True,
                "base_root_moved_count": 0,
                "unchanged_chain_changed_count": 0,
                "non_target_node_changed_count": 0,
                "non_target_coordinate_sha256": _sha("non-target"),
            },
            "interaction_recheck": [
                {
                    "interaction_component_sha256": component_id,
                    "candidate_root_count": candidate_count,
                    "affected_prism_count": 60,
                    "affected_core_element_count": 10,
                    "affected_core_tetra_count": 10,
                    "quality": copy.deepcopy(quality),
                }
            ],
            "final_quality": quality,
            "final_low_quality_prisms": {
                "count": 0,
                "records": [],
                "records_sha256": _canonical_sha256({"records": []}),
            },
            "observed_counts": {
                "projected_3d_element_count": 70,
                "prism_element_count": 60,
                "core_element_count": 10,
                "core_tetra_count": 10,
                "candidate_root_count": candidate_count,
                "changed_root_count": changed_count,
                "unchanged_root_count": unchanged_count,
                "applied_direction_count": changed_count,
                "searched_direction_count": 0,
            },
            "incomplete_reason": None,
        }
        self._rehash_discovery(discovery)
        expected = {
            "expected_approval": approval,
            "expected_projected_3d_elements": 70,
            "expected_prism_element_count": 60,
            "expected_core_element_count": 10,
            "expected_layer_count": 3,
            "expected_surface_count": 1,
            "expected_surface_schedules": surface_schedules,
            "expected_minimum_prism_scaled_jacobian": 0.01,
            "expected_minimum_core_tetra_gamma": 0.001,
        }
        return discovery, expected

    def test_two_independent_manifests_build_one_strict_consensus_approval(self) -> None:
        approval = self._load(self._write_pair())
        self.assertEqual(replay.APPROVAL_SCHEMA, approval["schema"])
        self.assertEqual("PASS", approval["status"])
        self.assertEqual(2, len(approval["source_audit_manifests"]))
        self.assertEqual(2, approval["consensus"]["attestation_count"])
        self.assertEqual("PASS", approval["consensus"]["status"])
        self.assertEqual(3, len(approval["root_contexts"]))
        self.assertEqual(
            sorted(record["root_coordinate_sha256"] for record in approval["root_contexts"]),
            [record["root_coordinate_sha256"] for record in approval["root_contexts"]],
        )
        self.assertEqual(
            [1001, 1002],
            sorted(
                source["worker_process_id"]
                for source in approval["source_audit_manifests"]
            ),
        )
        self.assertEqual(
            approval,
            replay.validate_direction_replay_approval(
                approval,
                **self.validation_expected,
            ),
        )

    def test_strict_file_sha_and_independent_source_identity_are_required(self) -> None:
        pair = self._write_pair()
        wrong_sha_pair = (pair[0], _sha("wrong-file"), pair[2], pair[3])
        with self.assertRaises(replay.CoarseDirectionReplayError):
            self._load(wrong_sha_pair)

        with self.assertRaises(replay.CoarseDirectionReplayError):
            self._load((pair[0], pair[1], pair[0], pair[1]))

    def test_duplicate_json_key_and_nonfinite_json_constant_are_rejected(self) -> None:
        for label, replacement in (
            ("duplicate", '"status":"PASS","status":"PASS"'),
            ("nan", '"characteristic_length_m":NaN'),
        ):
            with self.subTest(label=label):
                first = self._manifest(process_id=1001, ordinal=1)
                second = self._manifest(process_id=1002, ordinal=2)
                first_path, _first_sha = self._write_manifest(f"{label}-first", first)
                second_path, second_sha = self._write_manifest(
                    f"{label}-second", second
                )
                raw = first_path.read_text(encoding="utf-8")
                if label == "duplicate":
                    raw = raw.replace('"status":"PASS"', replacement, 1)
                else:
                    raw = raw.replace('"characteristic_length_m":0.4', replacement, 1)
                first_path.write_text(raw, encoding="utf-8")
                first_sha = hashlib.sha256(first_path.read_bytes()).hexdigest()
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    self._load((first_path, first_sha, second_path, second_sha))

    def test_audit_only_scope_cannot_be_promoted_or_contain_written_outputs(self) -> None:
        mutations = (
            lambda value: value.update({"audit_only": False}),
            lambda value: value.update({"calibration_PASS_authorized": True}),
            lambda value: value.update({"production_mesh_eligible": True}),
            lambda value: value.update({"mesh_written": True}),
            lambda value: value.update({"su2_called": True}),
            lambda value: value.update({"paraview_called": True}),
            lambda value: value.update({"external_commands": [["gmsh"]]}),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    self._load(self._write_pair(second_mutation=mutation))

        approval = self._load(self._write_pair())
        for field, unsafe_value in (
            ("audit_only_direction_replay_authorized", False),
            ("mesh_write_authorized", True),
            ("calibration_PASS_authorized", True),
            ("production_mesh_eligible", True),
        ):
            with self.subTest(approval_field=field):
                tampered = copy.deepcopy(approval)
                tampered[field] = unsafe_value
                self._rehash_approval(tampered)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    replay.validate_direction_replay_approval(
                        tampered,
                        **self.validation_expected,
                    )

    def test_consensus_rejects_any_proof_difference_between_attestations(self) -> None:
        def changed_direction(value) -> None:
            value["repair_discovery"]["search"]["changed_roots"][0][
                "selected_direction"
            ] = [0.0, 0.0, 1.0]

        def changed_state_b(value) -> None:
            value["repair_discovery"]["search"]["coordinate_replay"][
                "state_b_first_coordinate_sha256"
            ] = _sha("different-state-b")
            value["repair_discovery"]["search"]["coordinate_replay"][
                "state_b_restored_coordinate_sha256"
            ] = _sha("different-state-b")

        def changed_quality(value) -> None:
            quality = _quality(minimum_prism=0.021)
            value["repair_discovery"]["final_quality"] = quality
            replay_evidence = value["repair_discovery"]["search"][
                "coordinate_replay"
            ]
            replay_evidence["state_b_first_quality_sha256"] = quality[
                "quality_sha256"
            ]
            replay_evidence["state_b_restored_quality_sha256"] = quality[
                "quality_sha256"
            ]

        def changed_stable_observation(value) -> None:
            value["repair_discovery"]["stable_observation_sha256"] = _sha(
                "different-stable-observation"
            )

        def changed_aba_replay(value) -> None:
            value["repair_discovery"]["search"]["aba_replay"][
                "state_b_sha256"
            ] = _sha("different-aba-state-b")

        def changed_interaction_topology(value) -> None:
            value["repair_discovery"]["search"]["interaction_topology"][
                "affected_prism_count"
            ] = 59

        def changed_component_recheck(value) -> None:
            value["repair_discovery"]["search"]["final_component_recheck"][
                "quality_evaluation_count"
            ] = 2

        def changed_low_quality_inventory(value) -> None:
            value["repair_discovery"]["search"]["final_low_quality_prisms"][
                "lineage_quality_query_count"
            ] = 2

        def changed_observed_count(value) -> None:
            value["repair_discovery"]["observed_counts"][
                "projected_3d_element_count"
            ] = 71

        def changed_subdivision(value) -> None:
            value["repair_discovery"]["subdivision"][
                "surface_schedule_binding_sha256"
            ] = _sha("different-schedule-binding")

        for mutation in (
            changed_direction,
            changed_state_b,
            changed_quality,
            changed_stable_observation,
            changed_aba_replay,
            changed_interaction_topology,
            changed_component_recheck,
            changed_low_quality_inventory,
            changed_observed_count,
            changed_subdivision,
        ):
            with self.subTest(mutation=mutation.__name__):
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    self._load(self._write_pair(second_mutation=mutation))

    def test_changed_root_uses_exact_canonical_float_hex(self) -> None:
        approval = self._load(self._write_pair())
        changed = approval["changed_roots"][0]
        self.assertEqual(
            [0.0.hex(), 1.0.hex(), 0.0.hex()],
            changed["selected_direction_float_hex"],
        )
        self.assertEqual(
            (0.5).hex(), changed["selected_interior_weight_float_hex"]
        )

        tampered = copy.deepcopy(approval)
        tampered["changed_roots"][0]["selected_direction_float_hex"][0] = "0x0p+0"
        self._rehash_approval(tampered)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_direction_replay_approval(
                tampered,
                **self.validation_expected,
            )

    def test_validator_rejects_relaxed_thresholds_and_inconsistent_counts(self) -> None:
        approval = self._load(self._write_pair())
        mutations = (
            lambda value: value["source_minimum_growth_final_quality"].update(
                {"minimum_prism_scaled_jacobian_for_pass": 0.009}
            ),
            lambda value: value["source_minimum_growth_final_quality"].update(
                {"minimum_core_tetra_gamma_for_pass": 0.0009}
            ),
            lambda value: value["source_minimum_growth_final_quality"].update(
                {"prism_below_threshold_element_count": -1}
            ),
            lambda value: value["source_minimum_growth_final_quality"].update(
                {"core_element_count": 9}
            ),
            lambda value: value.update({"candidate_root_count": 4}),
            lambda value: value.update({"changed_root_count": 2}),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                tampered = copy.deepcopy(approval)
                mutation(tampered)
                self._rehash_approval(tampered)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    replay.validate_direction_replay_approval(
                        tampered,
                        **self.validation_expected,
                    )

    def test_physical_replay_validates_exact_schedule_and_quality_evidence(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)
        self.assertEqual(
            discovery,
            replay.validate_physical_schedule_replay_discovery(
                discovery, **expected
            ),
        )

    def test_physical_original_direction_allows_only_evidenced_binary64_roundoff(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)

        def set_runtime_first_component(value, runtime_value):
            compatibility = value["fixed_direction_contract"][
                "original_direction_compatibility"
            ]
            record = compatibility["records"][0]
            consensus = [
                float.fromhex(component)
                for component in record[
                    "consensus_original_direction_float_hex"
                ]
            ]
            runtime = list(consensus)
            runtime[0] = runtime_value
            deltas = [
                actual - reference
                for actual, reference in zip(runtime, consensus)
            ]
            maximum = max(abs(component) for component in deltas)
            record["runtime_original_direction_float_hex"] = [
                component.hex() for component in runtime
            ]
            record["maximum_absolute_difference_float_hex"] = maximum.hex()
            record["l2_difference_float_hex"] = math.sqrt(
                math.fsum(component * component for component in deltas)
            ).hex()
            record["dot_product_float_hex"] = math.fsum(
                actual * reference
                for actual, reference in zip(runtime, consensus)
            ).hex()
            record["matched_exactly"] = False
            record["within_absolute_tolerance"] = True
            compatibility["exact_match_root_count"] -= 1
            compatibility["roundoff_match_root_count"] += 1
            compatibility["records_sha256"] = _canonical_sha256(
                {"records": compatibility["records"]}
            )
            unsigned = copy.deepcopy(compatibility)
            unsigned.pop("compatibility_sha256")
            compatibility["compatibility_sha256"] = _canonical_sha256(unsigned)
            self._rehash_discovery(value)

        allowed = copy.deepcopy(discovery)
        set_runtime_first_component(
            allowed, math.nextafter(1.0, 0.0)
        )
        replay.validate_physical_schedule_replay_discovery(
            allowed, **expected
        )

        excessive = copy.deepcopy(discovery)
        set_runtime_first_component(excessive, 1.0 - 5.0e-13)
        with self.assertRaisesRegex(
            replay.CoarseDirectionReplayError, "binary64 roundoff"
        ):
            replay.validate_physical_schedule_replay_discovery(
                excessive, **expected
            )

    def test_physical_changed_roots_are_an_exact_unique_consensus_projection(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)
        mutations = (
            lambda value: value["fixed_direction_contract"]["changed_roots"][0].update(
                {"root_coordinate_sha256": _sha("unapproved-root")}
            ),
            lambda value: value["fixed_direction_contract"]["changed_roots"].append(
                copy.deepcopy(value["fixed_direction_contract"]["changed_roots"][0])
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                self._rehash_discovery(tampered)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    replay.validate_physical_schedule_replay_discovery(
                        tampered, **expected
                    )

        two_changed = copy.deepcopy(approval)
        second_context = next(
            context
            for context in two_changed["root_contexts"]
            if context["changed"] is False
        )
        second_context["changed"] = True
        context_unsigned = copy.deepcopy(second_context)
        context_unsigned.pop("root_context_sha256")
        second_context["root_context_sha256"] = _canonical_sha256(
            context_unsigned
        )
        second_root = copy.deepcopy(two_changed["changed_roots"][0])
        second_root["root_coordinate_sha256"] = second_context[
            "root_coordinate_sha256"
        ]
        second_root["root_context_sha256"] = second_context[
            "root_context_sha256"
        ]
        two_changed["changed_roots"].append(second_root)
        two_changed["changed_roots"].sort(
            key=lambda record: record["root_coordinate_sha256"]
        )
        two_changed["changed_root_count"] = 2
        two_changed["consensus"]["changed_roots_sha256"] = _canonical_sha256(
            two_changed["changed_roots"]
        )
        consensus_unsigned = copy.deepcopy(two_changed["consensus"])
        consensus_unsigned.pop("consensus_sha256")
        two_changed["consensus"]["consensus_sha256"] = _canonical_sha256(
            consensus_unsigned
        )
        self._rehash_approval(two_changed)
        replay.validate_direction_replay_approval(
            two_changed, **self.validation_expected
        )
        two_discovery, two_expected = self._physical_fixture(two_changed)
        two_discovery["fixed_direction_contract"]["changed_roots"].reverse()
        self._rehash_discovery(two_discovery)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                two_discovery, **two_expected
            )

    def test_physical_quality_hash_fields_counts_and_tet4_gate_are_strict(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)

        observed = copy.deepcopy(discovery)
        observed["observed_counts"]["core_tetra_count"] = 9
        self._rehash_discovery(observed)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                observed, **expected
            )

        extra_field = copy.deepcopy(discovery)
        extra_field["final_quality"]["unexpected"] = 0
        self._rehash_quality(extra_field["final_quality"])
        extra_field["coordinate_replay"]["state_b_first_quality_sha256"] = (
            extra_field["final_quality"]["quality_sha256"]
        )
        extra_field["coordinate_replay"]["state_b_second_quality_sha256"] = (
            extra_field["final_quality"]["quality_sha256"]
        )
        self._rehash_discovery(extra_field)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                extra_field, **expected
            )

        wrong_tetra = copy.deepcopy(discovery)
        wrong_tetra["final_quality"]["core_tetra_count"] = 9
        self._rehash_quality(wrong_tetra["final_quality"])
        wrong_tetra["coordinate_replay"]["state_b_first_quality_sha256"] = (
            wrong_tetra["final_quality"]["quality_sha256"]
        )
        wrong_tetra["coordinate_replay"]["state_b_second_quality_sha256"] = (
            wrong_tetra["final_quality"]["quality_sha256"]
        )
        self._rehash_discovery(wrong_tetra)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                wrong_tetra, **expected
            )

        stale_hash = copy.deepcopy(discovery)
        stale_hash["interaction_recheck"][0]["quality"][
            "minimum_prism_scaled_jacobian"
        ] = 0.03
        self._rehash_discovery(stale_hash)
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                stale_hash, **expected
            )

    def test_physical_interaction_components_are_complete_unique_and_coherent(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)
        mutations = (
            lambda value: value["interaction_recheck"].append(
                copy.deepcopy(value["interaction_recheck"][0])
            ),
            lambda value: value["interaction_recheck"][0].update(
                {"interaction_component_sha256": _sha("unknown-component")}
            ),
            lambda value: value["interaction_recheck"][0].update(
                {"candidate_root_count": 2}
            ),
            lambda value: value["interaction_recheck"][0].update(
                {"affected_core_tetra_count": 9}
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                self._rehash_discovery(tampered)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    replay.validate_physical_schedule_replay_discovery(
                        tampered, **expected
                    )

    def test_physical_root_schedule_assignments_are_recomputed_from_surfaces(self) -> None:
        approval = self._load(self._write_pair())
        discovery, expected = self._physical_fixture(approval)
        mutations = (
            lambda value: value["schedule_application"][
                "root_schedule_assignments"
            ].reverse(),
            lambda value: value["schedule_application"][
                "root_schedule_assignments"
            ][0].update({"selected_total_thickness_float_hex": (0.2).hex()}),
            lambda value: value["schedule_application"].update(
                {"baseline_surface_schedules_sha256": _sha("wrong-schedules")}
            ),
            lambda value: value["schedule_application"].update(
                {"root_schedule_assignment_count": 2}
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(discovery)
                mutation(tampered)
                assignments = tampered["schedule_application"][
                    "root_schedule_assignments"
                ]
                tampered["schedule_application"][
                    "root_schedule_assignments_sha256"
                ] = _canonical_sha256(
                    {"root_schedule_assignments": assignments}
                )
                self._rehash_discovery(tampered)
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    replay.validate_physical_schedule_replay_discovery(
                        tampered, **expected
                    )

        wrong_expected = copy.deepcopy(expected)
        wall = next(iter(wrong_expected["expected_surface_schedules"]))
        wrong_expected["expected_surface_schedules"][wall][-1] = 0.31
        with self.assertRaises(replay.CoarseDirectionReplayError):
            replay.validate_physical_schedule_replay_discovery(
                discovery, **wrong_expected
            )

        shared_wall = copy.deepcopy(discovery)
        shared_expected = copy.deepcopy(expected)
        original_wall = next(
            iter(shared_expected["expected_surface_schedules"])
        )
        second_wall = _sha("second-incident-wall")
        second_schedule = [0.1, 0.2, 0.25]
        shared_expected["expected_surface_schedules"][second_wall] = (
            second_schedule
        )
        shared_expected["expected_surface_count"] = 2
        schedule = shared_wall["schedule_application"]
        schedule["surface_count"] = 2
        schedule["baseline_surface_schedules_sha256"] = _canonical_sha256(
            {
                "surface_schedules": dict(
                    sorted(
                        shared_expected[
                            "expected_surface_schedules"
                        ].items()
                    )
                )
            }
        )
        schedule["minimum_surface_total_thickness_m"] = 0.25
        schedule["maximum_surface_total_thickness_m"] = 0.3
        for assignment in schedule["root_schedule_assignments"]:
            assignment["incident_wall_surface_fingerprints"] = sorted(
                [original_wall, second_wall]
            )
            assignment["cumulative_heights_sha256"] = _canonical_sha256(
                {
                    "cumulative_heights_float_hex": [
                        value.hex() for value in second_schedule
                    ]
                }
            )
            assignment["selected_total_thickness_float_hex"] = (
                second_schedule[-1].hex()
            )
        schedule["root_schedule_assignments_sha256"] = _canonical_sha256(
            {
                "root_schedule_assignments": schedule[
                    "root_schedule_assignments"
                ]
            }
        )
        self._rehash_discovery(shared_wall)
        replay.validate_physical_schedule_replay_discovery(
            shared_wall, **shared_expected
        )

    def test_source_attestation_requires_preimport_job_limit_and_argv_identity(self) -> None:
        mutations = (
            lambda value: value["worker_isolation"]["worker"].update(
                {"hard_limit_installed_before_heavy_import": False}
            ),
            lambda value: value["worker_isolation"]["worker"].update(
                {"configured_process_memory_limit_bytes": self.worker_limit - 1}
            ),
            lambda value: value["worker_isolation"]["worker_memory"][
                "hard_limit"
            ]["job_object"].update({"limit_flags": 0x0100}),
            lambda value: value["worker_isolation"]["worker_memory"][
                "hard_limit"
            ].update(
                {"requested_process_memory_limit_bytes": self.worker_limit - 1}
            ),
            lambda value: value["worker_isolation"]["worker"].update(
                {"normalized_resource_matches_raw": False}
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(replay.CoarseDirectionReplayError):
                    self._load(self._write_pair(second_mutation=mutation))

        pair = self._write_pair()
        second = json.loads(pair[2].read_text(encoding="utf-8"))
        second["run_evidence"]["worker_argv"][-1] = str(
            self.root / "different-output"
        )
        pair[2].write_text(
            json.dumps(
                second,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        second_sha = hashlib.sha256(pair[2].read_bytes()).hexdigest()
        with self.assertRaises(replay.CoarseDirectionReplayError):
            self._load((pair[0], pair[1], pair[2], second_sha))


if __name__ == "__main__":
    unittest.main()
