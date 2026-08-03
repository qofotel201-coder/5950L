from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from cfdpipe.boundary_layer_smoke import (
    _apply_one_layer_orientation_cone_subdivision,
    _discover_repair_source_triangle_groups,
)
from cfdpipe.boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
)
from cfdpipe.coarse_repair_audit import (
    CoarseRepairAuditError,
    PHYSICAL_HOMOTOPY_PURPOSE,
    PHYSICAL_HOMOTOPY_REQUEST,
    RESOURCE_ABORT_MANIFEST_NAME,
    RESOURCE_ABORT_MANIFEST_SCHEMA,
    SCHEDULE_CONTINUATION_PURPOSE,
    SCHEDULE_CONTINUATION_REQUEST,
    _canonical_sha256,
    _physical_surface_schedule_values,
    _validate_discovery,
    load_and_validate_physical_homotopy_audit_manifest,
    make_coarse_repair_audit_strategy_config,
    publish_coarse_repair_resource_abort_manifest,
    run_coarse_repair_audit,
    validate_coarse_repair_discovery,
    validate_coarse_repair_resource_abort_manifest,
)
from cfdpipe.coarse_direction_replay import (
    validate_physical_schedule_homotopy_endpoint,
)
from cfdpipe.coarse_schedule_feasibility import make_minimum_growth_endpoint


def _identifier(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _counts(*, ambiguity_count: int = 0, changed_count: int = 1) -> dict[str, int]:
    return {
        "initial_bad_prism_count": 1,
        "negative_volume_prism_count": 1,
        "cone_node_count": 1,
        "full_layer_bad_prism_count": 0,
        "initial_below_threshold_prism_count": 1,
        "repair_candidate_prism_count": 1,
        "bad_source_triangle_count": 1,
        "strict_bad_source_triangle_count": 0,
        "preferred_strict_failure_root_count": 0,
        "smoothing_group_count": 1,
        "candidate_smoothed_root_count": 1,
        "owner_ambiguity_count": ambiguity_count,
        "changed_smoothed_root_count": changed_count,
        "source_prism_count": 2,
        "projected_3d_element_count": 100,
    }


def _discovery(*, incomplete: bool = False) -> dict:
    wall_inventory = sorted(_identifier(f"wall-{index}") for index in range(48))
    triangle = _identifier("triangle")
    owner = _identifier("owner")
    candidate = _identifier("candidate")
    candidate_two = _identifier("candidate-two")
    counts = _counts(
        ambiguity_count=1 if incomplete else 0,
        changed_count=0 if incomplete else 1,
    )
    signature = {
        "observed_counts": counts,
        "wall_surface_fingerprint_inventory": wall_inventory,
        "initial_bad_one_layer_prisms": [
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": wall_inventory[0],
            }
        ],
        "reversed_one_layer_prisms": [
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": wall_inventory[0],
            }
        ],
        "repair_sites": [
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": wall_inventory[0],
                "assignment_method": (
                    "AUDIT_ONLY_UNRESOLVED"
                    if incomplete
                    else "contains_unique_sharp_root"
                ),
                "owner_root_coordinate_sha256": None if incomplete else owner,
                "candidate_owner_root_coordinate_sha256": (
                    sorted([candidate, candidate_two]) if incomplete else []
                ),
                "neighbour_root_coordinate_sha256": ([] if incomplete else [candidate]),
                "protected_root_coordinate_sha256": [],
            }
        ],
    }
    result = {
        "schema": "cfdpipe.coarse_repair_discovery.v1",
        "status": "INCOMPLETE" if incomplete else "PASS",
        "profile_complete": not incomplete,
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "runtime_mesh_tags_hardcoded": False,
        "runtime_tags_are_audit_only": True,
        "repair_application_performed": True,
        "repair_application_scope": (
            "IN_MEMORY_PRE_SMOOTHING_AUDIT_ONLY"
            if incomplete
            else "IN_MEMORY_COMPLETE_AUDIT_ONLY"
        ),
        "in_memory_mesh_mutation_performed": True,
        "final_smoothing_application_performed": not incomplete,
        "observed_counts": counts,
        "stable_signature": signature,
        "stable_signature_sha256": _canonical_sha256(signature),
        "owner_ambiguities": (
            [
                {
                    "source_triangle_sha256": triangle,
                    "wall_surface_fingerprint": wall_inventory[0],
                    "reason": "MULTIPLE_SHARP_ROOTS_WITHOUT_UNIQUE_STABLE_OWNER",
                    "candidate_root_coordinate_sha256": sorted(
                        [candidate, candidate_two]
                    ),
                }
            ]
            if incomplete
            else []
        ),
    }
    if incomplete:
        result["incomplete_reason"] = (
            "AMBIGUOUS_REPAIR_OWNER_REQUIRES_EXTERNAL_APPROVAL"
        )
    return result


class _Logger:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.started = False
        self.messages = list(messages if messages is not None else ["audit log"])

    def start(self) -> None:
        self.started = True

    def get(self) -> list[str]:
        return list(self.messages)

    def stop(self) -> None:
        self.started = False


class _Model:
    def __init__(self) -> None:
        self.names: list[str] = []

    def add(self, name: str) -> None:
        self.names.append(name)


class _Gmsh:
    __version__ = "test"
    __file__ = __file__

    def __init__(self, logger_messages: list[str] | None = None) -> None:
        self.logger = _Logger(logger_messages)
        self.model = _Model()
        self.initialized = False
        self.clear_called = False
        self.finalize_called = False
        self.write_calls: list[str] = []

    def isInitialized(self) -> bool:
        return self.initialized

    def initialize(self, *, readConfigFiles: bool) -> None:
        self.initialized = True
        self.read_config_files = readConfigFiles

    def clear(self) -> None:
        self.clear_called = True

    def finalize(self) -> None:
        self.finalize_called = True
        self.initialized = False

    def write(self, path: str) -> None:
        self.write_calls.append(path)


class _PartialInitGmsh(_Gmsh):
    def initialize(self, *, readConfigFiles: bool) -> None:
        super().initialize(readConfigFiles=readConfigFiles)
        raise RuntimeError("partial initialize")


class _FalseCheckerPartialInitGmsh(_Gmsh):
    def isInitialized(self) -> bool:
        return False

    def initialize(self, *, readConfigFiles: bool) -> None:
        self.read_config_files = readConfigFiles
        raise RuntimeError("partial initialize with false checker")


class _RaisingCheckerPartialInitGmsh(_Gmsh):
    def __init__(self) -> None:
        super().__init__()
        self.initialize_failed = False

    def isInitialized(self) -> bool:
        if self.initialize_failed:
            raise RuntimeError("post-initialize checker failed")
        return False

    def initialize(self, *, readConfigFiles: bool) -> None:
        self.read_config_files = readConfigFiles
        self.initialize_failed = True
        raise RuntimeError("partial initialize with raising checker")


class _AbsentCheckerPartialInitGmsh(_Gmsh):
    isInitialized = None

    def initialize(self, *, readConfigFiles: bool) -> None:
        self.read_config_files = readConfigFiles
        raise RuntimeError("partial initialize without checker")


class _Strategy:
    def __init__(self, discovery: dict | None = None, error: Exception | None = None):
        self.discovery = discovery
        self.error = error
        self.build_calls = 0

    def build(self, gmsh, source, config, staging):
        self.build_calls += 1
        del gmsh, source, staging
        self.seen_config = config
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.discovery)

    def evidence(self, phase: str):
        return {"phase": phase, "runtime_tag_audit": 123}


class _WritingStrategy(_Strategy):
    def build(self, gmsh, source, config, staging):
        self.build_calls += 1
        del source
        target = staging / "forbidden.msh"
        try:
            gmsh.write(target)
        except Exception:
            pass
        self.seen_config = config
        return copy.deepcopy(self.discovery)


class CoarseRepairAuditTests(unittest.TestCase):
    def test_physical_surface_schedule_projection_uses_cumulative_values(self):
        fingerprints = [_identifier("surface-a"), _identifier("surface-b")]
        binding = {
            "surface_schedules": {
                fingerprint: {
                    "surface_fingerprint_id": fingerprint,
                    "cumulative_heights_m": [0.1, 0.25, 0.5],
                    "unrelated_bound_metadata": "preserved-outside-projection",
                }
                for fingerprint in reversed(fingerprints)
            }
        }
        projected = _physical_surface_schedule_values(
            binding,
            expected_surface_count=2,
            expected_layer_count=3,
        )
        self.assertEqual(list(projected), sorted(fingerprints))
        self.assertEqual(
            projected,
            {fingerprint: [0.1, 0.25, 0.5] for fingerprint in fingerprints},
        )

        stale = copy.deepcopy(binding)
        stale["surface_schedules"][fingerprints[0]][
            "cumulative_heights_m"
        ] = [0.1, 0.1, 0.5]
        with self.assertRaisesRegex(CoarseRepairAuditError, "not monotone"):
            _physical_surface_schedule_values(
                stale,
                expected_surface_count=2,
                expected_layer_count=3,
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.brep"
        self.source.write_bytes(b"read-only BREP")
        self.source.chmod(stat.S_IREAD)
        self.contract = {
            "schema": "cfdpipe.coarse_mesh.v1",
            "normalized_config_sha256": _identifier("contract"),
            "calibration_characteristic_lengths_m": [0.4, 0.25],
            "pipeline_brep_path": str(self.source.resolve()),
            "provenance": {"pipeline_brep_sha256": self._file_hash(self.source)},
            "wall_surface_fingerprints": [
                _identifier(f"wall-{index}") for index in range(48)
            ],
            "boundary_layer_design": {
                "layer_count": 60,
                "first_layer_height_m": 1.0e-6,
                "growth_ratio": 1.2,
            },
        }
        self.binding = {
            "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
            "status": "PASS",
            "coarse_contract_sha256": self.contract["normalized_config_sha256"],
            "binding_sha256": _identifier("binding"),
        }
        self.binding_validator = mock.patch(
            "cfdpipe.coarse_repair_audit.validate_boundary_layer_schedule_binding",
            side_effect=lambda contract, binding: copy.deepcopy(dict(binding)),
        )
        self.binding_validator.start()
        self.addCleanup(self.binding_validator.stop)
        self.projection = {
            "path": str(self.source.resolve()),
            "sha256": self._file_hash(self.source),
            "config_sha256": _identifier("config"),
            "projected_3d_elements": 100,
        }

    def tearDown(self) -> None:
        self.source.chmod(stat.S_IWRITE | stat.S_IREAD)
        self.temporary.cleanup()

    @staticmethod
    def _file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _resource_abort_command(self, evidence: Path) -> Path:
        path = evidence / "worker.command.json"
        path.write_text(
            json.dumps(
                {
                    "executable": str((self.root / "python.exe").resolve()),
                    "args": ["-m", "cfdpipe.coarse_repair_audit_worker"],
                    "cwd": str(self.root.resolve()),
                    "start_time": "2026-08-02T18:45:59Z",
                    "end_time": "2026-08-02T18:46:52Z",
                    "returncode": 3221225786,
                    "status_hex": "0xC000013A",
                    "timed_out": False,
                    "interrupted": False,
                    "resource_aborted": True,
                    "resource_abort_reason": (
                        "physical available memory is below protected reserve"
                    ),
                    "output_complete": True,
                    "runner_error": None,
                    "termination_method": "resource_abort_graceful",
                    "graceful_termination_attempted": True,
                    "graceful_termination_succeeded": True,
                    "hard_kill_attempted": False,
                    "hard_kill_succeeded": False,
                },
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_resource_abort_manifest_is_atomic_fail_only_and_does_not_create_output(
        self,
    ) -> None:
        evidence = self.root / "evidence"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        output = self.root / "formal-output"

        manifest = publish_coarse_repair_resource_abort_manifest(
            evidence_directory=evidence,
            command_metadata_path=command,
            source_brep_path=self.source,
            expected_source_sha256=self._file_hash(self.source),
            output_directory=output,
        )

        path = evidence / RESOURCE_ABORT_MANIFEST_NAME
        self.assertTrue(path.is_file())
        self.assertFalse(output.exists())
        self.assertEqual(RESOURCE_ABORT_MANIFEST_SCHEMA, manifest["schema"])
        self.assertEqual("FAIL", manifest["status"])
        self.assertFalse(manifest["downstream_consumable"])
        self.assertFalse(manifest["calibration_PASS_authorized"])
        self.assertFalse(manifest["production_mesh_eligible"])
        self.assertEqual(
            "UNKNOWN_NOT_PROVEN",
            manifest["gmsh_lifecycle"]["finalize_status"],
        )
        self.assertIsNone(manifest["gmsh_lifecycle"]["finalize_called"])
        self.assertTrue(
            manifest["gmsh_lifecycle"][
                "output_capture_is_not_finalize_evidence"
            ]
        )
        self.assertEqual("0xC000013A", manifest["command_evidence"]["status_hex"])
        self.assertEqual(
            self._file_hash(command), manifest["command_evidence"]["sha256"]
        )
        self.assertTrue(
            manifest["source_brep_after_abort"]["matches_expected_sha256"]
        )
        self.assertTrue(manifest["source_brep_after_abort"]["read_only"])
        self.assertEqual("MISSING", manifest["output_inventory"]["formal_manifest"]["state"])
        self.assertEqual(0, manifest["output_inventory"]["recursive_file_count"])
        self.assertFalse(
            any(item.name.startswith(f".{RESOURCE_ABORT_MANIFEST_NAME}.") for item in evidence.iterdir())
        )
        self.assertEqual(
            manifest,
            validate_coarse_repair_resource_abort_manifest(
                manifest,
                expected_evidence_directory=evidence,
                expected_command_metadata_path=command,
                expected_source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                expected_output_directory=output,
            ),
        )
        with self.assertRaisesRegex(CoarseRepairAuditError, "already exists"):
            publish_coarse_repair_resource_abort_manifest(
                evidence_directory=evidence,
                command_metadata_path=command,
                source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                output_directory=output,
            )

    def test_resource_abort_manifest_inventories_partial_mesh_and_downstream_files(
        self,
    ) -> None:
        evidence = self.root / "evidence-partial"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        output = self.root / "partial-output"
        nested = output / "nested"
        nested.mkdir(parents=True)
        (output / "partial.msh").write_bytes(b"mesh")
        (nested / "case.cfg").write_text("cfg", encoding="utf-8")
        (nested / "partial.vtu").write_bytes(b"vtu")
        (output / "coarse_repair_audit_manifest.json").write_text(
            "{incomplete", encoding="utf-8"
        )

        manifest = publish_coarse_repair_resource_abort_manifest(
            evidence_directory=evidence,
            command_metadata_path=command,
            source_brep_path=self.source,
            expected_source_sha256=self._file_hash(self.source),
            output_directory=output,
        )

        inventory = manifest["output_inventory"]
        self.assertTrue(inventory["exists"])
        self.assertEqual(4, inventory["recursive_file_count"])
        self.assertEqual(1, inventory["mesh_artifact_count"])
        self.assertEqual(1, inventory["su2_artifact_count"])
        self.assertEqual(1, inventory["paraview_artifact_count"])
        self.assertEqual(2, inventory["downstream_artifact_count"])
        self.assertEqual("INCOMPLETE", inventory["formal_manifest"]["state"])
        self.assertIsNotNone(inventory["formal_manifest"]["parse_error"])
        self.assertFalse(manifest["downstream_consumable"])

    def test_resource_abort_manifest_rejects_legacy_or_rehashed_termination_evidence(
        self,
    ) -> None:
        evidence = self.root / "evidence-legacy"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        metadata = json.loads(command.read_text(encoding="utf-8"))
        del metadata["hard_kill_succeeded"]
        command.write_text(json.dumps(metadata), encoding="utf-8")
        target = evidence / RESOURCE_ABORT_MANIFEST_NAME
        with self.assertRaisesRegex(CoarseRepairAuditError, "lacks"):
            publish_coarse_repair_resource_abort_manifest(
                evidence_directory=evidence,
                command_metadata_path=command,
                source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                output_directory=self.root / "missing-output",
            )
        self.assertFalse(target.exists())

        command = self._resource_abort_command(evidence)
        metadata = json.loads(command.read_text(encoding="utf-8"))
        metadata["status_hex"] = "0x00000001"
        command.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(CoarseRepairAuditError, "complete"):
            publish_coarse_repair_resource_abort_manifest(
                evidence_directory=evidence,
                command_metadata_path=command,
                source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                output_directory=self.root / "missing-output",
            )
        self.assertFalse(target.exists())

        command = self._resource_abort_command(evidence)
        metadata = json.loads(command.read_text(encoding="utf-8"))
        metadata["termination_method"] = "CTRL_BREAK_EVENT"
        command.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(CoarseRepairAuditError, "complete"):
            publish_coarse_repair_resource_abort_manifest(
                evidence_directory=evidence,
                command_metadata_path=command,
                source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                output_directory=self.root / "missing-output",
            )
        self.assertFalse(target.exists())

    def test_resource_abort_manifest_rejects_rehashed_noncanonical_utc(self) -> None:
        evidence = self.root / "evidence-time"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        output = self.root / "missing-time-output"
        manifest = publish_coarse_repair_resource_abort_manifest(
            evidence_directory=evidence,
            command_metadata_path=command,
            source_brep_path=self.source,
            expected_source_sha256=self._file_hash(self.source),
            output_directory=output,
        )
        tampered = copy.deepcopy(manifest)
        tampered["published_at_utc"] = "2026-08-02T18:46:52+00:00"
        unsigned = {
            key: value
            for key, value in tampered.items()
            if key != "manifest_sha256"
        }
        tampered["manifest_sha256"] = _canonical_sha256(unsigned)
        with self.assertRaisesRegex(CoarseRepairAuditError, "status, policy"):
            validate_coarse_repair_resource_abort_manifest(
                tampered,
                expected_evidence_directory=evidence,
                expected_command_metadata_path=command,
                expected_source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                expected_output_directory=output,
            )

    def test_resource_abort_manifest_derives_hard_kill_method_from_flags(self) -> None:
        evidence = self.root / "evidence-hard-kill"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        metadata = json.loads(command.read_text(encoding="utf-8"))
        metadata.update(
            {
                "termination_method": "resource_abort_hard_kill",
                "graceful_termination_succeeded": False,
                "hard_kill_attempted": True,
                "hard_kill_succeeded": True,
            }
        )
        command.write_text(json.dumps(metadata), encoding="utf-8")
        manifest = publish_coarse_repair_resource_abort_manifest(
            evidence_directory=evidence,
            command_metadata_path=command,
            source_brep_path=self.source,
            expected_source_sha256=self._file_hash(self.source),
            output_directory=self.root / "hard-kill-output",
        )
        self.assertEqual(
            "resource_abort_hard_kill",
            manifest["command_evidence"]["termination_method"],
        )
        self.assertTrue(manifest["command_evidence"]["hard_kill_succeeded"])

    def test_resource_abort_manifest_rejects_complete_formal_manifest(self) -> None:
        evidence = self.root / "evidence-complete"
        evidence.mkdir()
        command = self._resource_abort_command(evidence)
        output = self.root / "complete-output"
        output.mkdir()
        snapshot = {
            "path": str(self.source.resolve()),
            "sha256": self._file_hash(self.source),
            "size_bytes": self.source.stat().st_size,
            "read_only": True,
        }
        (output / "coarse_repair_audit_manifest.json").write_text(
            json.dumps(
                {
                    "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
                    "status": "FAIL",
                    "ended_at_utc": "2026-08-02T18:46:52Z",
                    "gmsh_session": {
                        "finalize_attempted": True,
                        "finalize_called": False,
                        "cleanup_errors": ["forced termination"],
                    },
                    "source_after": snapshot,
                    "source_unchanged": True,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CoarseRepairAuditError, "complete formal"):
            publish_coarse_repair_resource_abort_manifest(
                evidence_directory=evidence,
                command_metadata_path=command,
                source_brep_path=self.source,
                expected_source_sha256=self._file_hash(self.source),
                output_directory=output,
            )
        self.assertFalse((evidence / RESOURCE_ABORT_MANIFEST_NAME).exists())

    @staticmethod
    def _config_factory(contract, characteristic, *, local_schedule_binding):
        config = {
            "schema": "cfdpipe.coarse_mesh.v1",
            "status": "CONFIGURED",
            "smoke_only": False,
            "calibration_only": False,
            "repair_audit_only": True,
            "projection_only": False,
            "contract_mode": "coarse_repair_audit_only",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "characteristic_length_m": characteristic,
            "coarse_contract_sha256": contract["normalized_config_sha256"],
            "wall_surface_fingerprints": list(
                contract["wall_surface_fingerprints"]
            ),
            "layer_count": contract["boundary_layer_design"]["layer_count"],
            "first_layer_height_m": contract["boundary_layer_design"][
                "first_layer_height_m"
            ],
            "growth_ratio": contract["boundary_layer_design"]["growth_ratio"],
            "local_boundary_layer_schedule": copy.deepcopy(
                dict(local_schedule_binding)
            ),
        }
        config["normalized_config_sha256"] = _canonical_sha256(config)
        return config

    @staticmethod
    def _projection_validator(record, **kwargs):
        del kwargs
        return dict(record)

    def _run(self, gmsh, strategy):
        return run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=strategy,
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=self._config_factory,
            projection_validator=self._projection_validator,
        )

    def _endpoint_config_factory(
        self,
        contract,
        characteristic,
        *,
        local_schedule_binding,
        schedule_feasibility_endpoint,
    ):
        self.assertEqual(
            "minimum-growth-existing-direction-repair",
            schedule_feasibility_endpoint,
        )
        config = self._config_factory(
            contract,
            characteristic,
            local_schedule_binding=local_schedule_binding,
        )
        config.pop("normalized_config_sha256")
        config["schedule_feasibility_endpoint"] = make_minimum_growth_endpoint(
            baseline_binding_sha256=local_schedule_binding["binding_sha256"],
            first_layer_height_m=contract["boundary_layer_design"][
                "first_layer_height_m"
            ],
            layer_count=contract["boundary_layer_design"]["layer_count"],
        )
        config["audit_purpose"] = (
            "minimum_growth_plus_existing_direction_repair"
        )
        config["combined_endpoint_only"] = True
        config["normalized_config_sha256"] = _canonical_sha256(config)
        return config

    def _physical_strategy_base(self) -> dict:
        result = self._config_factory(
            self.contract,
            0.4,
            local_schedule_binding=self.binding,
        )
        result["quality_improvement"] = {
            "minimum_prism_scaled_jacobian": 0.01,
            "minimum_core_tetra_gamma": 0.001,
        }
        return result

    def _physical_replay_approval(self) -> dict:
        return {
            "approval_sha256": _identifier("approval"),
            "projection_manifest_sha256": _identifier("projection"),
            "source_minimum_growth_final_quality": {
                "prism_element_count": 80,
                "core_element_count": 20,
            },
        }

    def test_physical_homotopy_strategy_is_deterministic_and_exclusive(self):
        approval = self._physical_replay_approval()
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=self._physical_strategy_base(),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit.validate_direction_replay_approval",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ):
            strategy = make_coarse_repair_audit_strategy_config(
                self.contract,
                0.4,
                local_schedule_binding=self.binding,
                schedule_feasibility_endpoint=PHYSICAL_HOMOTOPY_REQUEST,
                direction_replay_approval=approval,
            )

        endpoint = validate_physical_schedule_homotopy_endpoint(
            strategy["physical_schedule_homotopy_endpoint"],
            expected_binding_sha256=self.binding["binding_sha256"],
            expected_first_layer_height_m=self.contract[
                "boundary_layer_design"
            ]["first_layer_height_m"],
            expected_layer_count=self.contract["boundary_layer_design"][
                "layer_count"
            ],
        )
        self.assertEqual(PHYSICAL_HOMOTOPY_PURPOSE, strategy["audit_purpose"])
        self.assertTrue(strategy["fixed_direction_replay_only"])
        self.assertTrue(strategy["fixed_direction_homotopy_only"])
        self.assertTrue(strategy["combined_endpoint_only"])
        self.assertEqual(endpoint, strategy["schedule_feasibility_endpoint"])
        self.assertEqual(
            endpoint, strategy["physical_schedule_homotopy_endpoint"]
        )
        self.assertFalse(strategy["mesh_written"])
        self.assertFalse(strategy["su2_called"])
        self.assertFalse(strategy["paraview_called"])

    def test_physical_homotopy_requires_direction_replay_approval(self):
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=self._physical_strategy_base(),
        ):
            with self.assertRaisesRegex(
                CoarseRepairAuditError, "requires fixed-direction approval"
            ):
                make_coarse_repair_audit_strategy_config(
                    self.contract,
                    0.4,
                    local_schedule_binding=self.binding,
                    schedule_feasibility_endpoint=PHYSICAL_HOMOTOPY_REQUEST,
                )

    def test_first_frontier_direction_strategy_is_deterministic_and_exclusive(self):
        approval = self._physical_replay_approval()
        evidence = {
            "sha256": _identifier("homotopy-manifest"),
            "discovery": {"source": "validated-homotopy"},
        }
        continuation_endpoint = {
            "schema": "test.frontier-direction-endpoint.v1",
            "endpoint_sha256": _identifier("frontier-direction-endpoint"),
        }
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=self._physical_strategy_base(),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit.validate_direction_replay_approval",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit."
            "make_schedule_frontier_direction_endpoint",
            return_value=copy.deepcopy(continuation_endpoint),
        ) as make_endpoint, mock.patch(
            "cfdpipe.coarse_repair_audit."
            "validate_schedule_frontier_direction_endpoint",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ) as validate_endpoint:
            first = make_coarse_repair_audit_strategy_config(
                self.contract,
                0.4,
                local_schedule_binding=self.binding,
                schedule_feasibility_endpoint=SCHEDULE_CONTINUATION_REQUEST,
                direction_replay_approval=approval,
                homotopy_audit_evidence=evidence,
            )
            second = make_coarse_repair_audit_strategy_config(
                self.contract,
                0.4,
                local_schedule_binding=self.binding,
                schedule_feasibility_endpoint=SCHEDULE_CONTINUATION_REQUEST,
                direction_replay_approval=approval,
                homotopy_audit_evidence=evidence,
            )

        self.assertEqual(first, second)
        self.assertEqual(
            SCHEDULE_CONTINUATION_PURPOSE, first["audit_purpose"]
        )
        self.assertTrue(first["fixed_direction_replay_only"])
        self.assertTrue(first["combined_endpoint_only"])
        self.assertTrue(
            first["first_frontier_direction_continuation_only"]
        )
        self.assertNotIn("fixed_direction_homotopy_only", first)
        self.assertEqual(
            continuation_endpoint,
            first["schedule_direction_continuation_endpoint"],
        )
        self.assertEqual(
            first["physical_schedule_homotopy_endpoint"],
            first["schedule_feasibility_endpoint"],
        )
        self.assertFalse(first["mesh_written"])
        self.assertFalse(first["su2_called"])
        self.assertFalse(first["paraview_called"])
        self.assertEqual(2, make_endpoint.call_count)
        self.assertEqual(2, validate_endpoint.call_count)

    def test_first_frontier_direction_requires_validated_homotopy_source(self):
        approval = self._physical_replay_approval()
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=self._physical_strategy_base(),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit.validate_direction_replay_approval",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ):
            with self.assertRaisesRegex(
                CoarseRepairAuditError, "validated homotopy audit"
            ):
                make_coarse_repair_audit_strategy_config(
                    self.contract,
                    0.4,
                    local_schedule_binding=self.binding,
                    schedule_feasibility_endpoint=(
                        SCHEDULE_CONTINUATION_REQUEST
                    ),
                    direction_replay_approval=approval,
                )

    def test_homotopy_source_loader_rehashes_and_revalidates_first_frontier(self):
        projection = {"evidence": "projection"}
        binding = {"evidence": "schedule"}
        approval = {"evidence": "approval"}
        endpoint = {"evidence": "homotopy-endpoint"}
        discovery = {
            "schema": (
                "cfdpipe.coarse_physical_schedule_fixed_direction_"
                "homotopy_discovery.v1"
            ),
            "status": "INCOMPLETE",
            "profile_complete": False,
            "maximum_tested_pass_fraction_float_hex": "0x0.0p+0",
            "first_tested_fail_fraction_float_hex": "0x1.0000000000000p-12",
        }
        with tempfile.TemporaryDirectory() as raw:
            output_root = Path(raw).resolve()
            source = (
                output_root
                / "physical-homotopy-002"
                / "coarse_repair_audit_manifest.json"
            )
            source.parent.mkdir()
            manifest = {
                "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
                "status": "INCOMPLETE",
                "audit_only": True,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "su2_called": False,
                "paraview_called": False,
                "external_commands": [],
                "error": None,
                "audit_purpose": PHYSICAL_HOMOTOPY_PURPOSE,
                "schedule_feasibility_endpoint_requested": (
                    PHYSICAL_HOMOTOPY_REQUEST
                ),
                "coarse_contract_sha256": "a" * 64,
                "characteristic_length_m": 0.4,
                "projection_evidence": projection,
                "local_schedule_binding": binding,
                "direction_replay_approval": approval,
                "physical_schedule_homotopy_endpoint": endpoint,
                "strategy_config": {
                    "audit_purpose": PHYSICAL_HOMOTOPY_PURPOSE,
                    "fixed_direction_replay_only": True,
                    "fixed_direction_homotopy_only": True,
                    "combined_endpoint_only": True,
                    "owner_free_direction_replay_approval": approval,
                    "schedule_feasibility_endpoint": endpoint,
                    "physical_schedule_homotopy_endpoint": endpoint,
                },
                "repair_discovery": {"raw": "source"},
            }
            source.write_text(
                json.dumps(manifest, allow_nan=False), encoding="utf-8"
            )
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            with mock.patch(
                "cfdpipe.coarse_repair_audit."
                "validate_physical_schedule_homotopy_discovery",
                return_value=copy.deepcopy(discovery),
            ) as validate:
                evidence = load_and_validate_physical_homotopy_audit_manifest(
                    source,
                    source_sha256,
                    output_root=output_root,
                    expected_contract_sha256="a" * 64,
                    expected_characteristic_length_m=0.4,
                    expected_projection_evidence=projection,
                    expected_local_schedule_binding=binding,
                    expected_approval=approval,
                    expected_endpoint=endpoint,
                    expected_projected_3d_elements=100,
                    expected_prism_element_count=80,
                    expected_core_element_count=20,
                    expected_layer_count=60,
                    expected_surface_count=48,
                    expected_surface_schedules={"wall": [1.0]},
                    expected_minimum_prism_scaled_jacobian=0.01,
                    expected_minimum_core_tetra_gamma=0.001,
                )
            self.assertEqual(str(source.resolve()), evidence["path"])
            self.assertEqual(source_sha256, evidence["sha256"])
            self.assertEqual("INCOMPLETE", evidence["manifest_status"])
            self.assertEqual(discovery, evidence["discovery"])
            validate.assert_called_once()
            with self.assertRaisesRegex(
                CoarseRepairAuditError, "identity is invalid"
            ):
                load_and_validate_physical_homotopy_audit_manifest(
                    source,
                    "f" * 64,
                    output_root=output_root,
                    expected_contract_sha256="a" * 64,
                    expected_characteristic_length_m=0.4,
                    expected_projection_evidence=projection,
                    expected_local_schedule_binding=binding,
                    expected_approval=approval,
                    expected_endpoint=endpoint,
                    expected_projected_3d_elements=100,
                    expected_prism_element_count=80,
                    expected_core_element_count=20,
                    expected_layer_count=60,
                    expected_surface_count=48,
                    expected_surface_schedules={"wall": [1.0]},
                    expected_minimum_prism_scaled_jacobian=0.01,
                    expected_minimum_core_tetra_gamma=0.001,
                )

    def test_ordinary_physical_replay_does_not_gain_homotopy_state(self):
        approval = self._physical_replay_approval()
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=self._physical_strategy_base(),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit.validate_direction_replay_approval",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ):
            strategy = make_coarse_repair_audit_strategy_config(
                self.contract,
                0.4,
                local_schedule_binding=self.binding,
                direction_replay_approval=approval,
            )

        self.assertFalse(strategy["combined_endpoint_only"])
        self.assertNotIn("schedule_feasibility_endpoint", strategy)
        self.assertNotIn("physical_schedule_homotopy_endpoint", strategy)
        self.assertNotIn("fixed_direction_homotopy_only", strategy)

    def test_physical_homotopy_discovery_reaches_authoritative_validator(self):
        approval = self._physical_replay_approval()
        binding = copy.deepcopy(self.binding)
        binding["surface_schedules"] = {
            fingerprint: {
                "surface_fingerprint_id": fingerprint,
                "cumulative_heights_m": [
                    self.contract["boundary_layer_design"][
                        "first_layer_height_m"
                    ]
                    * layer
                    for layer in range(
                        1,
                        self.contract["boundary_layer_design"]["layer_count"]
                        + 1,
                    )
                ],
            }
            for fingerprint in self.contract["wall_surface_fingerprints"]
        }
        discovery = {
            "schema": (
                "cfdpipe.coarse_physical_schedule_fixed_direction_"
                "homotopy_discovery.v1"
            ),
            "status": "INCOMPLETE",
            "profile_complete": False,
            "observed_counts": {"projected_3d_element_count": 100},
            "discovery_sha256": _identifier("homotopy-discovery"),
        }
        captured = {}

        def validate_homotopy(value, **kwargs):
            captured.update(copy.deepcopy(kwargs))
            return copy.deepcopy(dict(value))

        physical_strategy_base = self._physical_strategy_base()
        physical_strategy_base["local_boundary_layer_schedule"] = (
            copy.deepcopy(binding)
        )
        with mock.patch(
            "cfdpipe.coarse_repair_audit.make_coarse_strategy_config",
            return_value=physical_strategy_base,
        ), mock.patch(
            "cfdpipe.coarse_repair_audit.validate_direction_replay_approval",
            side_effect=lambda value, **kwargs: copy.deepcopy(dict(value)),
        ), mock.patch(
            "cfdpipe.coarse_repair_audit."
            "validate_physical_schedule_homotopy_discovery",
            side_effect=validate_homotopy,
        ):
            gmsh = _Gmsh()
            manifest = run_coarse_repair_audit(
                contract=self.contract,
                characteristic_length_m=0.4,
                strategy=_Strategy(discovery),
                local_schedule_binding=binding,
                projection_evidence=self.projection,
                gmsh_module=gmsh,
                schedule_feasibility_endpoint=PHYSICAL_HOMOTOPY_REQUEST,
                direction_replay_approval=approval,
                strategy_config_factory=make_coarse_repair_audit_strategy_config,
                projection_validator=self._projection_validator,
            )

        self.assertEqual(
            "INCOMPLETE", manifest["status"], msg=manifest.get("error")
        )
        self.assertTrue(gmsh.finalize_called)
        self.assertEqual(
            manifest["physical_schedule_homotopy_endpoint"],
            captured["expected_endpoint"],
        )
        self.assertEqual(
            {
                fingerprint: [
                    self.contract["boundary_layer_design"][
                        "first_layer_height_m"
                    ]
                    * layer
                    for layer in range(
                        1,
                        self.contract["boundary_layer_design"]["layer_count"]
                        + 1,
                    )
                ]
                for fingerprint in self.contract["wall_surface_fingerprints"]
            },
            captured["expected_surface_schedules"],
        )

    def test_combined_endpoint_is_hash_bound_and_reaches_strategy(self):
        gmsh = _Gmsh()
        strategy = _Strategy(_discovery())
        endpoint = "minimum-growth-existing-direction-repair"
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=strategy,
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            schedule_feasibility_endpoint=endpoint,
            strategy_config_factory=self._endpoint_config_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual("PASS", manifest["status"])
        self.assertEqual(endpoint, manifest["schedule_feasibility_endpoint_requested"])
        self.assertEqual(
            "minimum_growth_plus_existing_direction_repair",
            manifest["audit_purpose"],
        )
        self.assertEqual(
            manifest["strategy_config"]["schedule_feasibility_endpoint"],
            strategy.seen_config["schedule_feasibility_endpoint"],
        )
        self.assertTrue(gmsh.finalize_called)

    def test_stable_signature_preserves_repair_site_pairing_and_counts(self):
        discovery = validate_coarse_repair_discovery(_discovery())
        signature = discovery["stable_signature"]
        self.assertEqual(
            signature["observed_counts"], discovery["observed_counts"]
        )
        self.assertEqual(len(signature["repair_sites"]), 1)
        self.assertIn("source_triangle_sha256", signature["repair_sites"][0])
        self.assertIn("wall_surface_fingerprint", signature["repair_sites"][0])
        self.assertFalse(
            any("audit" in key for key in signature["repair_sites"][0])
        )
        discovery["observed_counts"]["initial_bad_prism_count"] = 999
        self.assertEqual(_discovery()["observed_counts"]["initial_bad_prism_count"], 1)

    def test_runtime_tag_in_stable_signature_is_rejected(self):
        discovery = _discovery()
        discovery["stable_signature"]["repair_sites"][0]["root_tag"] = 7
        discovery["stable_signature_sha256"] = _canonical_sha256(
            discovery["stable_signature"]
        )
        with self.assertRaisesRegex(Exception, "stable site schema|runtime"):
            validate_coarse_repair_discovery(discovery)

    def test_tampered_observed_count_invalidates_stable_signature(self):
        discovery = _discovery()
        discovery["observed_counts"]["initial_bad_prism_count"] = 2
        with self.assertRaisesRegex(Exception, "stable signature|observed counts"):
            _validate_discovery(discovery)

    def test_complete_profile_requires_one_nonambiguous_owner_per_site(self):
        discovery = _discovery()
        discovery["stable_signature"]["repair_sites"][0][
            "owner_root_coordinate_sha256"
        ] = None
        discovery["stable_signature_sha256"] = _canonical_sha256(
            discovery["stable_signature"]
        )
        with self.assertRaisesRegex(Exception, "assigned stable owner"):
            _validate_discovery(discovery)

    def test_observed_count_schema_rejects_extra_fields(self):
        discovery = _discovery()
        discovery["observed_counts"]["unexpected"] = 1
        discovery["stable_signature_sha256"] = _canonical_sha256(
            discovery["stable_signature"]
        )
        with self.assertRaisesRegex(Exception, "observed counts"):
            _validate_discovery(discovery)

    def test_incomplete_profile_requires_structured_ambiguity(self):
        discovery = _validate_discovery(_discovery(incomplete=True))
        self.assertEqual(discovery["status"], "INCOMPLETE")
        self.assertFalse(discovery["profile_complete"])
        self.assertTrue(discovery["in_memory_mesh_mutation_performed"])
        self.assertFalse(discovery["final_smoothing_application_performed"])
        self.assertTrue(discovery["repair_application_performed"])
        broken = _discovery(incomplete=True)
        broken["owner_ambiguities"] = []
        with self.assertRaisesRegex(Exception, "ambiguity"):
            _validate_discovery(broken)

    def test_discovery_grouping_records_ambiguous_stable_candidates(self):
        triangle = (1, 2, 3)
        label = _identifier("wall")
        triangle_id = _identifier("triangle")
        root_ids = {1: _identifier("root-1"), 2: _identifier("root-2")}
        groups, assignments, ambiguities = (
            _discover_repair_source_triangle_groups(
                [triangle],
                [1, 2],
                triangle_group_labels={triangle: label},
                triangle_stable_ids={triangle: triangle_id},
                root_stable_ids=root_ids,
                approved_ambiguous_owner_stable_ids={},
            )
        )
        self.assertEqual(groups, {})
        self.assertIsNone(assignments[triangle]["sharp_root_tag_audit"])
        self.assertEqual(
            ambiguities[0]["candidate_root_coordinate_sha256"],
            sorted(root_ids.values()),
        )

    def test_strict_failure_ambiguity_cannot_be_swallowed_by_unique_neighbour(self):
        direct = (1, 2, 3)
        strict_ambiguous = (2, 3, 4)
        triangles = [direct, strict_ambiguous]
        label = _identifier("wall")
        labels = {triangle: label for triangle in triangles}
        stable_ids = {
            direct: _identifier("direct"),
            strict_ambiguous: _identifier("strict-ambiguous"),
        }
        root_ids = {1: _identifier("root-1")}
        _, inferred, inferred_ambiguities = _discover_repair_source_triangle_groups(
            triangles,
            [1],
            triangle_group_labels=labels,
            triangle_stable_ids=stable_ids,
            root_stable_ids=root_ids,
            approved_ambiguous_owner_stable_ids={},
        )
        self.assertEqual(inferred_ambiguities, [])
        self.assertEqual(
            inferred[strict_ambiguous]["assignment_method"],
            "shares_edge_with_direct_sharp_root_triangle",
        )

        groups, assignments, ambiguities = _discover_repair_source_triangle_groups(
            triangles,
            [1],
            forced_unresolved_triangles=[strict_ambiguous],
            triangle_group_labels=labels,
            triangle_stable_ids=stable_ids,
            root_stable_ids=root_ids,
            approved_ambiguous_owner_stable_ids={},
        )
        self.assertEqual(groups, {1: {2, 3}})
        self.assertEqual(
            assignments[strict_ambiguous]["assignment_method"],
            "AUDIT_ONLY_UNRESOLVED",
        )
        self.assertEqual(
            ambiguities[0]["reason"],
            "STRICT_FAILURE_WITHOUT_UNIQUE_DIRECT_SHARP_ROOT",
        )
        self.assertTrue(ambiguities[0]["one_ring_owner_inference_suppressed"])

    def test_strict_repair_hook_default_remains_false(self):
        self.assertFalse(
            _apply_one_layer_orientation_cone_subdivision.__kwdefaults__[
                "audit_only"
            ]
        )

    def test_strategy_hash_and_lineage_fail_before_gmsh(self):
        def stale_hash_factory(contract, characteristic, *, local_schedule_binding):
            config = self._config_factory(
                contract,
                characteristic,
                local_schedule_binding=local_schedule_binding,
            )
            config["characteristic_length_m"] = characteristic + 0.01
            return config

        gmsh = _Gmsh()
        strategy = _Strategy(_discovery())
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=strategy,
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=stale_hash_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual("FAIL", manifest["status"])
        self.assertIn("config hash is stale", manifest["error"]["message"])
        self.assertFalse(manifest["gmsh_session"]["initialize_attempted"])
        self.assertEqual(0, strategy.build_calls)

        def stale_lineage_factory(
            contract, characteristic, *, local_schedule_binding
        ):
            config = self._config_factory(
                contract,
                characteristic,
                local_schedule_binding=local_schedule_binding,
            )
            config["coarse_contract_sha256"] = "f" * 64
            config.pop("normalized_config_sha256")
            config["normalized_config_sha256"] = _canonical_sha256(config)
            return config

        gmsh = _Gmsh()
        strategy = _Strategy(_discovery())
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=strategy,
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=stale_lineage_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual("FAIL", manifest["status"])
        self.assertIn("binding lineage is stale", manifest["error"]["message"])
        self.assertFalse(manifest["gmsh_session"]["initialize_attempted"])
        self.assertEqual(0, strategy.build_calls)

    def test_conflicting_audit_modes_fail_before_gmsh(self):
        for field in ("smoke_only", "calibration_only", "projection_only"):
            with self.subTest(field=field):
                def conflicting_factory(
                    contract,
                    characteristic,
                    *,
                    local_schedule_binding,
                    conflicting_field=field,
                ):
                    config = self._config_factory(
                        contract,
                        characteristic,
                        local_schedule_binding=local_schedule_binding,
                    )
                    config[conflicting_field] = True
                    config.pop("normalized_config_sha256")
                    config["normalized_config_sha256"] = _canonical_sha256(config)
                    return config

                gmsh = _Gmsh()
                strategy = _Strategy(_discovery())
                manifest = run_coarse_repair_audit(
                    contract=self.contract,
                    characteristic_length_m=0.4,
                    strategy=strategy,
                    local_schedule_binding=self.binding,
                    projection_evidence=self.projection,
                    gmsh_module=gmsh,
                    strategy_config_factory=conflicting_factory,
                    projection_validator=self._projection_validator,
                )
                self.assertEqual("FAIL", manifest["status"])
                self.assertIn("mode matrix", manifest["error"]["message"])
                self.assertFalse(manifest["gmsh_session"]["initialize_attempted"])
                self.assertEqual(0, strategy.build_calls)

    def test_authoritative_binding_failure_stops_before_gmsh(self):
        gmsh = _Gmsh()
        strategy = _Strategy(_discovery())
        with mock.patch(
            "cfdpipe.coarse_repair_audit.validate_boundary_layer_schedule_binding",
            side_effect=BoundaryLayerScheduleBindingError("forged binding"),
        ):
            manifest = run_coarse_repair_audit(
                contract=self.contract,
                characteristic_length_m=0.4,
                strategy=strategy,
                local_schedule_binding=self.binding,
                projection_evidence=self.projection,
                gmsh_module=gmsh,
                strategy_config_factory=self._config_factory,
                projection_validator=self._projection_validator,
            )
        self.assertEqual("FAIL", manifest["status"])
        self.assertIn("forged binding", manifest["error"]["message"])
        self.assertFalse(manifest["gmsh_session"]["initialize_attempted"])
        self.assertEqual(0, strategy.build_calls)

    def test_lifecycle_pass_never_authorizes_or_writes_mesh(self):
        gmsh = _Gmsh()
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=_Strategy(_discovery()),
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=self._config_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual(manifest["status"], "PASS")
        self.assertTrue(manifest["source_unchanged"])
        self.assertTrue(manifest["gmsh_session"]["finalize_called"])
        self.assertFalse(manifest["mesh_written"])
        self.assertFalse(manifest["calibration_PASS_authorized"])
        self.assertFalse(manifest["production_mesh_eligible"])
        self.assertEqual(manifest["external_commands"], [])
        self.assertEqual(gmsh.write_calls, [])
        self.assertEqual(
            manifest["gmsh_session"]["write_guard"]["write_attempt_count"], 0
        )
        self.assertEqual(
            manifest["gmsh_session"]["diagnostic_audit"]["status"], "PASS"
        )

    def test_strategy_exception_still_finalizes(self):
        gmsh = _Gmsh()
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=_Strategy(error=RuntimeError("boom")),
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=self._config_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIn("boom", manifest["error"]["message"])
        self.assertTrue(manifest["gmsh_session"]["finalize_called"])
        self.assertTrue(manifest["source_unchanged"])

    def test_partial_initialize_exception_still_finalizes(self):
        gmsh = _PartialInitGmsh()
        manifest = run_coarse_repair_audit(
            contract=self.contract,
            characteristic_length_m=0.4,
            strategy=_Strategy(_discovery()),
            local_schedule_binding=self.binding,
            projection_evidence=self.projection,
            gmsh_module=gmsh,
            strategy_config_factory=self._config_factory,
            projection_validator=self._projection_validator,
        )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIn("partial initialize", manifest["error"]["message"])
        self.assertTrue(manifest["gmsh_session"]["clear_called"])
        self.assertTrue(manifest["gmsh_session"]["finalize_called"])

    def test_finalize_attempted_when_partial_initialize_checker_is_false(self):
        gmsh = _FalseCheckerPartialInitGmsh()
        manifest = self._run(gmsh, _Strategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertFalse(manifest["gmsh_session"]["post_initialize_state"])
        self.assertTrue(manifest["gmsh_session"]["finalize_attempted"])
        self.assertTrue(gmsh.finalize_called)

    def test_finalize_attempted_when_partial_initialize_checker_raises(self):
        gmsh = _RaisingCheckerPartialInitGmsh()
        manifest = self._run(gmsh, _Strategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertIn(
            "post-initialize checker failed",
            manifest["gmsh_session"]["post_initialize_state_check_error"]["message"],
        )
        self.assertTrue(manifest["gmsh_session"]["finalize_attempted"])
        self.assertTrue(gmsh.finalize_called)

    def test_finalize_attempted_when_partial_initialize_checker_is_absent(self):
        gmsh = _AbsentCheckerPartialInitGmsh()
        manifest = self._run(gmsh, _Strategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertFalse(
            manifest["gmsh_session"]["post_initialize_state_check_attempted"]
        )
        self.assertTrue(manifest["gmsh_session"]["finalize_attempted"])
        self.assertTrue(gmsh.finalize_called)

    def test_fatal_gmsh_logger_message_forces_fail_and_is_preserved(self):
        gmsh = _Gmsh(["Error: NaN detected in mesh audit"])
        manifest = self._run(gmsh, _Strategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(
            manifest["gmsh_session"]["logger_messages"],
            ["Error: NaN detected in mesh audit"],
        )
        diagnostic = manifest["gmsh_session"]["diagnostic_audit"]
        self.assertEqual(diagnostic["status"], "FAIL")
        self.assertEqual(
            diagnostic["fatal_messages"], ["Error: NaN detected in mesh audit"]
        )

    def test_missing_gmsh_logger_cannot_pass(self):
        gmsh = _Gmsh()
        gmsh.logger = None
        manifest = self._run(gmsh, _Strategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        self.assertFalse(manifest["gmsh_session"]["logger_started"])
        self.assertEqual(
            manifest["gmsh_session"]["diagnostic_audit"]["failure_reason"],
            "GMSH_LOGGER_NOT_STARTED_OR_CAPTURED",
        )

    def test_gmsh_write_is_blocked_counted_and_cannot_be_swallowed(self):
        gmsh = _Gmsh()
        manifest = self._run(gmsh, _WritingStrategy(_discovery()))
        self.assertEqual(manifest["status"], "FAIL")
        write_guard = manifest["gmsh_session"]["write_guard"]
        self.assertTrue(write_guard["installed"])
        self.assertEqual(write_guard["write_attempt_count"], 1)
        self.assertEqual(gmsh.write_calls, [])
        self.assertFalse((self.root / "forbidden.msh").exists())


if __name__ == "__main__":
    unittest.main()
