from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from cfdpipe.coarse_mesh import (
    CoarseMeshError,
    build_coarse_calibration,
    make_coarse_strategy_config,
    normalize_coarse_mesh_config,
)
from cfdpipe.boundary_layer_smoke import _repair_layer_schedule
from cfdpipe import coarse_mesh_worker


ROOT = Path(__file__).resolve().parents[1]


def _document() -> dict:
    return tomllib.loads((ROOT / "config" / "coarse_mesh.toml").read_text("utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calibration_contract(*, source_brep: Path, timeout: float = 10.0) -> dict:
    return {
        "pipeline_brep_path": str(source_brep),
        "provenance": {"pipeline_brep_sha256": _sha256(source_brep)},
        "normalized_config_sha256": "a" * 64,
        "calibration_characteristic_lengths_m": [0.25, 0.20],
        "solver_marker_names": ["farfield", "wall"],
        "calibration_maximum_elapsed_seconds": timeout,
    }


def _strategy_config() -> dict:
    return {
        "max_3d_elements": 100,
        "normalized_config_sha256": "b" * 64,
    }


def _projection_evidence(*, projected_count: int = 3) -> dict:
    return {
        "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
        "status": "PASS",
        "path": str(ROOT / "projection_manifest.json"),
        "sha256": "c" * 64,
        "config_sha256": "d" * 64,
        "coarse_contract_sha256": "a" * 64,
        "characteristic_length_m": 0.25,
        "projected_3d_elements": projected_count,
        "evidence_sha256": "e" * 64,
    }


def _audit_payload() -> dict:
    return {
        "status": "PASS",
        "node_count": 8,
        "element_count_3d": 3,
        "element_types_3d": {"Tetrahedron 4": 3},
        "external_face_count": 2,
        "marker_face_counts": {"farfield": 1, "wall": 1},
        "shared_interface_face_count": 1,
        "shared_interface_region_pairs": {"fluid_core_1|fluid_core_2": 1},
        "requested_layer_count": 60,
        "layer_audit": {"column_count": 1},
    }


class _FakeModel:
    def add(self, name: str) -> None:
        self.name = name


class _FakeGmsh:
    __version__ = "test"
    __file__ = None

    def __init__(self) -> None:
        self.model = _FakeModel()

    @staticmethod
    def write(path: str) -> None:
        target = Path(path)
        target.write_bytes(b"msh\n" if target.suffix == ".msh" else b"su2\n")

    @staticmethod
    def open(path: str) -> None:
        if not Path(path).is_file():
            raise AssertionError("readback path does not exist")


class _FakeStrategy:
    @staticmethod
    def build(*args, **kwargs) -> dict:
        return {}

    @staticmethod
    def readback(*args, **kwargs) -> dict:
        return {}

    @staticmethod
    def evidence(phase: str) -> dict:
        return {"phase": phase}


def _serialized(**overrides) -> dict:
    result = {
        "status": "PASS",
        "nelem": 3,
        "volume_element_types": {"Tetrahedron 4": 3},
        "marker_element_counts": {"farfield": 1, "wall": 1},
        "sha256": hashlib.sha256(b"su2\n").hexdigest(),
        "file_size_bytes": 4,
        "error": None,
    }
    result.update(overrides)
    return result


def _fake_session(gmsh, action, manifest, phase):
    result = action()
    manifest["gmsh_sessions"][phase]["finalize_called"] = True
    return result


def _finalizer(status: str):
    def finish(manifest, *args):
        manifest["diagnostic_quality_status"] = status
        manifest["requires_quality_improvement_before_production"] = status != "PASS"
        return {"status": status}

    return finish


class CoarseMeshContractTests(unittest.TestCase):
    def test_repository_contract_normalizes_to_sixty_layer_design(self) -> None:
        result = normalize_coarse_mesh_config(_document(), repository_root=ROOT)

        self.assertEqual(result["schema"], "cfdpipe.coarse_mesh.v1")
        self.assertEqual(result["case_id"], "M50_H21_A8_B0")
        self.assertEqual(result["target_3d_elements"], 5_000_000)
        self.assertEqual(result["target_element_range"], [4_750_000, 5_250_000])
        self.assertEqual(result["boundary_layer_design"]["layer_count"], 60)
        self.assertEqual(result["boundary_layer_maximum_clearance_fraction"], 0.40)
        self.assertEqual(len(result["clearance_evidence_contract_sha256"]), 64)
        self.assertEqual(
            result["calibration_characteristic_lengths_m"], [0.40, 0.32]
        )
        self.assertEqual(
            [
                group["name"]
                for group in result["coarse_additional_refinement_groups"]
            ],
            ["rear_top_shell_plc_recovery"],
        )
        recovery = result["coarse_additional_refinement_groups"][0]
        self.assertEqual(len(recovery["surface_fingerprint_ids"]), 6)
        self.assertEqual(recovery["minimum_size_m"], 0.225)
        self.assertEqual(recovery["distance_max_m"], 0.08)
        self.assertEqual(recovery["sampling"], 100)
        self.assertEqual(result["solver_marker_names"], [
            "front_conical_surface",
            "rear_outlet_1",
            "rear_outlet_2",
            "vehicle_internal_and_external_walls",
        ])
        self.assertFalse(result["run_su2"])
        self.assertFalse(result["run_paraview"])
        self.assertEqual(len(result["normalized_config_sha256"]), 64)

    def test_stale_project_hash_fails_closed(self) -> None:
        document = _document()
        document["provenance"]["project_sha256"] = "0" * 64
        with self.assertRaisesRegex(CoarseMeshError, "project_sha256"):
            normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_yplus_evidence_value_must_match_hash_bound_json(self) -> None:
        document = _document()
        document["evidence"]["solution_yplus"]["maximum_yplus"] = 0.5
        with self.assertRaisesRegex(CoarseMeshError, r"maximum y\+"):
            normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_layer_dependent_repair_count_must_match_design(self) -> None:
        document = _document()
        document["post_mesh_repair"][
            "expected_initial_below_threshold_prism_count"
        ] = 90
        with self.assertRaisesRegex(CoarseMeshError, "layer-dependent repair"):
            normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_unsafe_scope_and_cell_tolerance_are_rejected(self) -> None:
        for mutate in (
            lambda value: value["scope"].__setitem__("run_su2", True),
            lambda value: value["scope"].__setitem__("nproc", 2),
            lambda value: value["cell_count"].__setitem__("relative_tolerance", 0.5),
        ):
            with self.subTest(mutate=mutate):
                document = copy.deepcopy(_document())
                mutate(document)
                with self.assertRaises(CoarseMeshError):
                    normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_resource_constants_cannot_be_relaxed(self) -> None:
        document = _document()
        document["resource"]["safety_factor"] = 1.0
        with self.assertRaisesRegex(CoarseMeshError, "resource"):
            normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_coarse_recovery_group_rejects_name_numeric_and_shape_tampering(self) -> None:
        mutations = (
            lambda group: group.__setitem__("name", "unapproved_recovery"),
            lambda group: group.__setitem__("minimum_size_m", 0.321),
            lambda group: group.__setitem__("distance_max_m", 0.0),
            lambda group: group.__setitem__("sampling", 0),
            lambda group: group.__setitem__("runtime_surface_tag", 8),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                document = _document()
                mutate(document["meshing"]["additional_refinement_groups"][0])
                with self.assertRaisesRegex(
                    CoarseMeshError, "additional refinement group"
                ):
                    normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_coarse_recovery_group_rejects_nonwall_and_duplicate_fingerprints(self) -> None:
        nonwall = _document()
        nonwall["meshing"]["additional_refinement_groups"][0][
            "surface_fingerprint_ids"
        ][0] = "0" * 64
        with self.assertRaisesRegex(CoarseMeshError, "fingerprints are invalid"):
            normalize_coarse_mesh_config(nonwall, repository_root=ROOT)

        duplicate = _document()
        fingerprints = duplicate["meshing"]["additional_refinement_groups"][0][
            "surface_fingerprint_ids"
        ]
        fingerprints[-1] = fingerprints[0]
        with self.assertRaisesRegex(CoarseMeshError, "fingerprints are invalid"):
            normalize_coarse_mesh_config(duplicate, repository_root=ROOT)

    def test_clearance_evidence_hash_ignores_unrelated_worker_resource_policy(self) -> None:
        baseline = normalize_coarse_mesh_config(_document(), repository_root=ROOT)
        changed_document = _document()
        changed_document["resource"]["worker_monitor_margin_bytes"] += 256 * 1024**2
        changed = normalize_coarse_mesh_config(
            changed_document,
            repository_root=ROOT,
        )

        self.assertNotEqual(
            baseline["normalized_config_sha256"],
            changed["normalized_config_sha256"],
        )
        self.assertEqual(
            baseline["clearance_evidence_contract"],
            changed["clearance_evidence_contract"],
        )
        self.assertEqual(
            baseline["clearance_evidence_contract_sha256"],
            changed["clearance_evidence_contract_sha256"],
        )

    def test_clearance_fraction_must_leave_opposing_wall_core(self) -> None:
        for value in (0.0, 0.5, 0.75):
            with self.subTest(value=value):
                document = _document()
                document["boundary_layer"]["maximum_clearance_fraction"] = value
                with self.assertRaisesRegex(CoarseMeshError, "clearance fraction"):
                    normalize_coarse_mesh_config(document, repository_root=ROOT)

    def test_shared_strategy_schedule_supports_coarse_layer_count_without_weakening_smoke(self) -> None:
        schedule = _repair_layer_schedule(
            {
                "first_layer_height_m": 2.7645771273320875e-7,
                "growth_ratio": 1.2,
                "layer_count": 60,
            }
        )
        self.assertEqual(len(schedule), 60)
        self.assertGreater(schedule[-1], 0.05)

    def test_calibration_strategy_config_is_separate_from_smoke_contract(self) -> None:
        contract = normalize_coarse_mesh_config(_document(), repository_root=ROOT)
        first_point = contract["calibration_characteristic_lengths_m"][0]
        with self.assertRaisesRegex(CoarseMeshError, "hash-bound local schedule"):
            make_coarse_strategy_config(contract, first_point)
        strategy = make_coarse_strategy_config(
            contract, first_point, projection_only=True
        )

        self.assertEqual(strategy["schema"], "cfdpipe.coarse_mesh.v1")
        self.assertEqual(strategy["contract_mode"], "coarse_calibration")
        self.assertEqual(strategy["layer_count"], 60)
        self.assertEqual(strategy["max_3d_elements"], 750_000)
        self.assertAlmostEqual(
            strategy["construction_first_layer_height_m"],
            contract["base_smoke_contract"][
                "construction_first_layer_height_m"
            ],
        )
        self.assertLess(
            strategy["construction_first_layer_height_m"],
            contract["boundary_layer_design"]["total_thickness_m"],
        )
        self.assertFalse(strategy["smoke_only"])
        self.assertFalse(strategy["production_mesh_eligible"])
        strategy_groups = strategy["quality_improvement"][
            "additional_refinement_groups"
        ]
        self.assertEqual(
            [group["name"] for group in strategy_groups],
            ["core_interface_sliver_regions", "rear_top_shell_plc_recovery"],
        )
        self.assertEqual(
            strategy_groups[-1], contract["coarse_additional_refinement_groups"][0]
        )
        self.assertEqual(len(strategy["normalized_config_sha256"]), 64)
        with self.assertRaisesRegex(CoarseMeshError, "not authorized"):
            make_coarse_strategy_config(contract, 0.19, projection_only=True)


class CoarseMeshCalibrationBuilderTests(unittest.TestCase):
    def _build(
        self,
        output: Path,
        allowed_root: Path,
        *,
        serialized: dict | None = None,
        finalizer_status: str = "PASS",
        elapsed_seconds: float = 5.0,
        peak_bytes: int | None = 4096,
        projected_count: int = 3,
    ) -> dict:
        audit = _audit_payload()
        source_brep = allowed_root / ".portable-test-source.brep"
        source_brep.write_bytes(b"portable coarse-mesh unit-test fixture\n")
        source_brep.chmod(stat.S_IREAD)
        try:
            with (
                mock.patch(
                    "cfdpipe.coarse_mesh.make_coarse_strategy_config",
                    return_value=_strategy_config(),
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh.validate_projection_evidence_record",
                    side_effect=lambda value, **kwargs: dict(value),
                ),
                mock.patch("cfdpipe.coarse_mesh._session", side_effect=_fake_session),
                mock.patch(
                    "cfdpipe.coarse_mesh._call_audit",
                    side_effect=[copy.deepcopy(audit), copy.deepcopy(audit)],
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh._strategy_evidence",
                    side_effect=lambda strategy, phase: {"phase": phase},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh._quality_gate_signature",
                    return_value={"status": "PASS"},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh._reject_fatal_gmsh_log",
                    return_value={"status": "PASS"},
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh.audit_su2_mesh",
                    return_value=copy.deepcopy(serialized or _serialized()),
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh._finalize_deferred_gmsh_log_diagnostics",
                    side_effect=_finalizer(finalizer_status),
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh._peak_working_set_bytes",
                    return_value=peak_bytes,
                ),
                mock.patch(
                    "cfdpipe.coarse_mesh.time.monotonic",
                    side_effect=[100.0, 100.0 + elapsed_seconds],
                ),
            ):
                return build_coarse_calibration(
                    contract=_calibration_contract(source_brep=source_brep),
                    characteristic_length_m=0.25,
                    output_directory=output,
                    allowed_output_root=allowed_root,
                    strategy=_FakeStrategy(),
                    projection_evidence=_projection_evidence(
                        projected_count=projected_count
                    ),
                    gmsh_module=_FakeGmsh(),
                    controller_argv=["unit-test"],
                )
        finally:
            if source_brep.exists():
                source_brep.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_serialized_failure_preserves_singular_error_and_retained_files(self) -> None:
        failure = _serialized(
            status="FAIL",
            error={
                "type": "SU2MeshAuditError",
                "message": "sentinel marker parse failure",
            },
        )
        with tempfile.TemporaryDirectory() as raw:
            allowed = Path(raw) / "allowed"
            allowed.mkdir()
            output = allowed / "failed"
            with self.assertRaisesRegex(CoarseMeshError, "sentinel marker parse failure"):
                self._build(output, allowed, serialized=failure)
            manifest = json.loads(
                (output / "coarse_calibration_manifest.json").read_text("utf-8")
            )

        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(
            manifest["su2_validation"]["error"]["message"],
            "sentinel marker parse failure",
        )
        self.assertEqual(set(manifest["retained_artifacts"]), {"mesh.msh", "mesh.su2"})
        for record in manifest["retained_artifacts"].values():
            self.assertGreater(record["size_bytes"], 0)
            self.assertEqual(len(record["sha256"]), 64)
            self.assertFalse(record["validated"])
        self.assertEqual(manifest["outputs"], {})
        self.assertFalse(manifest["resource_sample_eligible"])

    def test_serialized_counts_and_types_must_match_generation_unconditionally(self) -> None:
        cases = (
            (_serialized(nelem=2), "element count"),
            (
                _serialized(volume_element_types={"Prism 6": 3}),
                "element type counts",
            ),
            (
                _serialized(marker_element_counts={"farfield": 2, "wall": 1}),
                "marker counts",
            ),
        )
        for serialized, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw:
                allowed = Path(raw) / "allowed"
                allowed.mkdir()
                output = allowed / "failed"
                with self.assertRaisesRegex(CoarseMeshError, expected):
                    self._build(output, allowed, serialized=serialized)
                manifest = json.loads(
                    (output / "coarse_calibration_manifest.json").read_text("utf-8")
                )
                self.assertEqual(manifest["status"], "FAIL")
                self.assertFalse(manifest["resource_sample_eligible"])

    def test_serialized_sha256_must_match_final_output_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            allowed = Path(raw) / "allowed"
            allowed.mkdir()
            output = allowed / "bad_hash"
            with self.assertRaisesRegex(CoarseMeshError, "SHA-256"):
                self._build(output, allowed, serialized=_serialized(sha256="0" * 64))
            manifest = json.loads(
                (output / "coarse_calibration_manifest.json").read_text("utf-8")
            )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["outputs"], {})
        self.assertEqual(
            manifest["retained_artifacts"]["mesh.su2"]["sha256"],
            hashlib.sha256(b"su2\n").hexdigest(),
        )

    def test_deferred_warning_cannot_be_published_as_manifest_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            allowed = Path(raw) / "allowed"
            allowed.mkdir()
            output = allowed / "warning"
            with self.assertRaisesRegex(CoarseMeshError, "deferred diagnostic"):
                self._build(output, allowed, finalizer_status="WARN")
            manifest = json.loads(
                (output / "coarse_calibration_manifest.json").read_text("utf-8")
            )
        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["diagnostic_quality_status"], "WARN")
        self.assertFalse(manifest["resource_sample_eligible"])

    def test_resource_sample_eligibility_requires_time_and_positive_peak(self) -> None:
        cases = (
            (5.0, 4096, True),
            (11.0, 4096, False),
            (5.0, None, False),
            (5.0, 0, False),
        )
        for elapsed, peak, expected in cases:
            with (
                self.subTest(elapsed=elapsed, peak=peak),
                tempfile.TemporaryDirectory() as raw,
            ):
                allowed = Path(raw) / "allowed"
                allowed.mkdir()
                manifest = self._build(
                    allowed / "pass",
                    allowed,
                    elapsed_seconds=elapsed,
                    peak_bytes=peak,
                )
                self.assertEqual(manifest["status"], "PASS")
                self.assertIs(manifest["resource_sample_eligible"], expected)
                self.assertEqual(
                    manifest["resource_sample_eligibility"]["elapsed_within_limit"],
                    elapsed <= 10.0,
                )
                self.assertEqual(
                    manifest["resource_sample_eligibility"]["peak_working_set_valid"],
                    isinstance(peak, int)
                    and not isinstance(peak, bool)
                    and peak > 0,
                )

    def test_public_builder_rejects_output_outside_root_or_existing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            allowed = temporary / "allowed"
            allowed.mkdir()
            existing = allowed / "existing"
            existing.mkdir()
            for output, message in (
                (temporary / "outside", "allowed output root"),
                (allowed, "strict child"),
                (existing, "must not already exist"),
            ):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(CoarseMeshError, message):
                        self._build(output, allowed)

    def test_public_builder_rejects_symlinked_output_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            allowed = temporary / "allowed"
            allowed.mkdir()
            linked_parent = allowed / "linked"
            linked_parent.mkdir()
            with (
                mock.patch(
                    "cfdpipe.coarse_mesh._is_link_like",
                    side_effect=lambda path: Path(path) == linked_parent,
                ),
                self.assertRaisesRegex(CoarseMeshError, "symbolic link"),
            ):
                self._build(linked_parent / "run", allowed)


class CoarseMeshWorkerTests(unittest.TestCase):
    GIB = 1024**3

    class _Memory:
        def __init__(self, events, *, fail_limit=False):
            self.events = events
            self.fail_limit = fail_limit

        def capture_system_physical_memory(self):
            self.events.append("system")
            return {"schema": "test.system", "status": "PASS", "metrics": {}}

        def capture_current_process_memory(self):
            self.events.append("process")
            return {"schema": "test.process", "status": "PASS", "metrics": {}}

        def install_current_process_memory_limit(self, limit_bytes):
            self.events.append(f"limit:{limit_bytes}")
            if self.fail_limit:
                error = RuntimeError("nested Job assignment failed")
                error.evidence = {
                    "status": "FAIL",
                    "error": {"message": "nested Job assignment failed"},
                }
                raise error
            return {
                "schema": "cfdpipe.worker_memory_limit.v1",
                "status": "PASS",
                "requested_process_memory_limit_bytes": limit_bytes,
                "job_object": {
                    "process_memory_limit_bytes": limit_bytes,
                    "process_assigned": True,
                    "handle_retained_for_process_lifetime": True,
                },
            }

    class _Strategy:
        events = []

        def __init__(self, markers, topology, **kwargs):
            self.events.append("strategy")

    def _fixture(self, temporary: Path) -> tuple[Path, Path, dict, Path, str]:
        (temporary / "config").mkdir()
        output_root = temporary / "runs" / "mesh" / "coarse"
        output_root.mkdir(parents=True)
        config = temporary / "config" / "coarse_mesh.toml"
        config.write_text(
            """
schema = "cfdpipe.coarse_mesh.v1"
status = "CONFIGURED"

[calibration]
characteristic_lengths_m = [0.30, 0.25]

[resource]
minimum_calibration_samples = 2
safety_factor = 1.30
os_physical_memory_reserve_bytes = 4294967296
minimum_disk_free_bytes = 21474836480
physical_memory_required = true
pagefile_cannot_rescue_physical_memory_failure = true
projection_worker_memory_limit_bytes = 2147483648
calibration_worker_memory_limit_bytes = 3221225472
worker_monitor_margin_bytes = 536870912
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (temporary / "config" / "markers.toml").write_text(
            "name = \"markers\"\n", encoding="utf-8"
        )
        (temporary / "config" / "topology_smoke.toml").write_text(
            "name = \"topology\"\n", encoding="utf-8"
        )
        raw_resource = tomllib.loads(config.read_text("utf-8"))["resource"]
        schedule_directory = output_root / "local_schedule_001"
        schedule_directory.mkdir()
        schedule = schedule_directory / "boundary_layer_local_schedule.json"
        schedule.write_text(
            json.dumps({"schema": "test.local.schedule", "status": "PASS"}),
            encoding="utf-8",
        )
        schedule_sha256 = hashlib.sha256(schedule.read_bytes()).hexdigest()
        projection_directory = output_root / "projection_first"
        projection_directory.mkdir()
        projection_manifest = projection_directory / "projection_manifest.json"
        projection_manifest.write_text(
            json.dumps({"schema": "test.projection", "status": "PASS"}),
            encoding="utf-8",
        )
        self.config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
        self.contract_sha256 = "d" * 64
        self.projection_manifest = projection_manifest
        self.projection_manifest_sha256 = hashlib.sha256(
            projection_manifest.read_bytes()
        ).hexdigest()
        return config, output_root, raw_resource, schedule, schedule_sha256

    def _projection_loader(self, *args, **kwargs):
        self.assertEqual(self.projection_manifest, Path(args[0]))
        self.assertEqual(self.projection_manifest_sha256, args[1])
        self.assertEqual(self.config_sha256, kwargs["expected_config_sha256"])
        self.assertEqual(
            self.contract_sha256,
            kwargs["expected_coarse_contract_sha256"],
        )
        return {
            "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
            "status": "PASS",
            "path": str(self.projection_manifest.resolve()),
            "sha256": self.projection_manifest_sha256,
            "size_bytes": self.projection_manifest.stat().st_size,
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract_sha256,
            "characteristic_length_m": 0.30,
            "projected_3d_elements": 123,
            "strictly_below_300000": True,
            "gmsh_finalize_called": True,
            "worker_job_object_verified": True,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
            "evidence_sha256": "f" * 64,
        }

    def _worker_lineage(self) -> dict:
        return {
            "config_sha256": self.config_sha256,
            "coarse_contract_sha256": self.contract_sha256,
            "projection_manifest_path": self.projection_manifest,
            "projection_manifest_sha256": self.projection_manifest_sha256,
            "projection_evidence_loader": self._projection_loader,
        }

    def _heavy(self, events, raw_resource, *, builder_error=None, bind_error=None):
        def normalize(document, *, repository_root):
            events.append("normalize")
            return {
                "normalized_config_sha256": self.contract_sha256,
                "resource": copy.deepcopy(raw_resource),
                "calibration_characteristic_lengths_m": [0.30, 0.25],
                "provenance": {
                    "markers_sha256": "a" * 64,
                    "topology_smoke_sha256": "b" * 64,
                },
            }

        def bind(*, coarse_contract, plan_path, plan_sha256):
            events.append("bind")
            if bind_error is not None:
                raise bind_error
            source = Path(plan_path).resolve(strict=True)
            return {
                "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
                "status": "PASS",
                "source_plan_path": str(source),
                "source_plan_sha256": plan_sha256,
                "source_plan_size_bytes": source.stat().st_size,
                "coarse_contract_sha256": "d" * 64,
                "surface_count": 48,
                "layer_count": 60,
                "binding_sha256": "e" * 64,
            }

        def builder(**kwargs):
            events.append("builder")
            self.assertEqual(
                "PASS", kwargs["local_schedule_binding"]["status"]
            )
            self.assertEqual(
                self.projection_manifest_sha256,
                kwargs["projection_evidence"]["sha256"],
            )
            output = Path(kwargs["output_directory"])
            output.mkdir()
            manifest = {
                "schema": "cfdpipe.coarse_mesh_calibration_manifest.v1",
                "status": "PASS" if builder_error is None else "FAIL",
                "generation_audit": {"element_count_3d": 123},
                "gmsh_sessions": {
                    "build": {"finalize_called": builder_error is None}
                },
            }
            (output / "coarse_calibration_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            if builder_error is not None:
                raise builder_error
            return manifest

        def loader():
            events.append("heavy_import")
            self._Strategy.events = events
            return coarse_mesh_worker._HeavyDependencies(
                normalize, bind, self._Strategy, builder
            )

        return loader

    def test_worker_installs_job_before_heavy_import_and_persists_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, raw_resource, schedule, schedule_sha256 = (
                self._fixture(root)
            )
            output = output_root / "unit-worker"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.25,
                    local_schedule_path=schedule,
                    local_schedule_sha256=schedule_sha256,
                    output_directory=output,
                    argv=["python", "-m", "cfdpipe.coarse_mesh_worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=self._heavy(events, raw_resource),
                )

            limit = 3 * self.GIB
            self.assertEqual(0, code)
            self.assertLess(events.index(f"limit:{limit}"), events.index("heavy_import"))
            self.assertEqual(
                ["system", "process", f"limit:{limit}", "system", "process"],
                events[:5],
            )
            self.assertEqual(
                [
                    "heavy_import",
                    "normalize",
                    "bind",
                    "strategy",
                    "builder",
                    "process",
                    "system",
                ],
                events[5:],
            )
            evidence_path = (
                output_root
                / "_worker_evidence"
                / output.name
                / coarse_mesh_worker.WORKER_EVIDENCE_NAME
            )
            saved = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual("PASS", saved["status"])
            self.assertTrue(
                saved["worker"]["hard_limit_installed_before_heavy_import"]
            )
            self.assertTrue(saved["worker"]["normalized_resource_matches_raw"])
            self.assertTrue(
                saved["worker"]["local_schedule_bound_after_heavy_import"]
            )
            self.assertEqual(
                schedule_sha256,
                saved["local_schedule"]["binding"]["source_plan_sha256"],
            )
            final = json.loads(
                (output / "coarse_calibration_manifest.json").read_text("utf-8")
            )
            self.assertEqual("PASS", final["worker_isolation"]["status"])
            self.assertEqual("PASS", manifest["worker_isolation"]["status"])
            json.dumps(saved, allow_nan=False)

    def test_job_failure_preserves_full_evidence_and_never_loads_heavy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, _, schedule, schedule_sha256 = self._fixture(root)
            output = output_root / "job-failure"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=schedule_sha256,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events, fail_limit=True),
                    heavy_loader=lambda: self.fail("heavy loader must not run"),
                )

            self.assertEqual(1, code)
            self.assertEqual(
                ["system", "process", f"limit:{3 * self.GIB}"], events
            )
            evidence_path = (
                output_root
                / "_worker_evidence"
                / output.name
                / coarse_mesh_worker.WORKER_EVIDENCE_NAME
            )
            saved = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual("FAIL", saved["status"])
            self.assertFalse(saved["worker"]["heavy_dependencies_imported"])
            self.assertIn("nested Job assignment failed", saved["worker_error"]["message"])
            self.assertEqual("FAIL", saved["worker_error"]["evidence"]["status"])
            self.assertFalse(output.exists())
            self.assertEqual("FAIL", manifest["status"])

    def test_normalized_resource_mismatch_stops_before_strategy_and_builder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, raw_resource, schedule, schedule_sha256 = (
                self._fixture(root)
            )
            output = output_root / "resource-mismatch"
            events = []
            changed = copy.deepcopy(raw_resource)
            changed["worker_monitor_margin_bytes"] += 1
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=schedule_sha256,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=self._heavy(events, changed),
                )

            self.assertEqual(1, code)
            self.assertIn("differ", manifest["worker_error"]["message"])
            self.assertNotIn("strategy", events)
            self.assertNotIn("builder", events)
            self.assertLess(events.index(f"limit:{3 * self.GIB}"), events.index("normalize"))

    def test_builder_failure_does_not_invent_finalize_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, raw_resource, schedule, schedule_sha256 = (
                self._fixture(root)
            )
            output = output_root / "builder-failure"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, _ = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=schedule_sha256,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=self._heavy(
                        events,
                        raw_resource,
                        builder_error=RuntimeError("gmsh build failed"),
                    ),
                )

            self.assertEqual(1, code)
            final = json.loads(
                (output / "coarse_calibration_manifest.json").read_text("utf-8")
            )
            self.assertFalse(final["gmsh_sessions"]["build"]["finalize_called"])
            self.assertEqual("FAIL", final["worker_isolation"]["status"])

    def test_existing_output_is_rejected_before_memory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, _, schedule, schedule_sha256 = self._fixture(root)
            output = output_root / "existing"
            output.mkdir()
            sentinel = output / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, _ = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=schedule_sha256,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=lambda: self.fail("heavy loader must not run"),
                )

            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_tampered_local_schedule_fails_before_job_and_heavy_import(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, _, schedule, stale_sha256 = self._fixture(root)
            schedule.write_text('{"status":"tampered"}\n', encoding="utf-8")
            output = output_root / "tampered-schedule"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=stale_sha256,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=lambda: self.fail("heavy loader must not run"),
                )

            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertFalse(output.exists())
            self.assertIn("does not exactly match", manifest["worker_error"]["message"])

    def test_schedule_outside_coarse_root_or_current_evidence_is_rejected(self) -> None:
        for location in ("outside", "current-evidence"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                config, output_root, _, _, _ = self._fixture(root)
                output = output_root / f"reject-{location}"
                if location == "outside":
                    schedule = root / "outside.json"
                else:
                    directory = output_root / "_worker_evidence" / output.name
                    directory.mkdir(parents=True)
                    schedule = directory / "schedule.json"
                schedule.write_text('{"status":"PASS"}\n', encoding="utf-8")
                digest = hashlib.sha256(schedule.read_bytes()).hexdigest()
                events = []
                with contextlib.redirect_stderr(io.StringIO()):
                    code, _ = coarse_mesh_worker.execute_calibration_worker(
                        config_path=config,
                        **self._worker_lineage(),
                        characteristic_length_m=0.30,
                        local_schedule_path=schedule,
                        local_schedule_sha256=digest,
                        output_directory=output,
                        argv=["worker"],
                        repository_root=root,
                        output_root=output_root,
                        memory_api=self._Memory(events),
                        heavy_loader=lambda: self.fail("heavy loader must not run"),
                    )

                self.assertEqual(1, code)
                self.assertEqual([], events)
                self.assertFalse(output.exists())

    def test_link_like_schedule_is_rejected_before_job(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, _, schedule, digest = self._fixture(root)
            output = output_root / "link-like"
            events = []
            original = coarse_mesh_worker._is_link_like

            def link_only(candidate):
                return Path(candidate) == schedule or original(Path(candidate))

            with (
                mock.patch.object(
                    coarse_mesh_worker, "_is_link_like", side_effect=link_only
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code, _ = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=digest,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=lambda: self.fail("heavy loader must not run"),
                )

            self.assertEqual(1, code)
            self.assertEqual([], events)

    def test_stale_binding_lineage_fails_after_job_but_before_builder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, raw_resource, schedule, digest = self._fixture(root)
            output = output_root / "stale-binding"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = coarse_mesh_worker.execute_calibration_worker(
                    config_path=config,
                    **self._worker_lineage(),
                    characteristic_length_m=0.30,
                    local_schedule_path=schedule,
                    local_schedule_sha256=digest,
                    output_directory=output,
                    argv=["worker"],
                    repository_root=root,
                    output_root=output_root,
                    memory_api=self._Memory(events),
                    heavy_loader=self._heavy(
                        events,
                        raw_resource,
                        bind_error=RuntimeError("stale local schedule lineage"),
                    ),
                )

            self.assertEqual(1, code)
            self.assertIn("bind", events)
            self.assertNotIn("strategy", events)
            self.assertNotIn("builder", events)
            self.assertLess(
                events.index(f"limit:{3 * self.GIB}"), events.index("bind")
            )
            self.assertIn("stale local schedule lineage", manifest["worker_error"]["message"])
            evidence = json.loads(
                (
                    output_root
                    / "_worker_evidence"
                    / output.name
                    / coarse_mesh_worker.WORKER_EVIDENCE_NAME
                ).read_text("utf-8")
            )
            self.assertEqual(digest, evidence["local_schedule"]["pre_job_validation"]["sha256"])
            self.assertFalse(evidence["worker"]["builder_called"])

    def test_parser_requires_explicit_schedule_path_and_sha256(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            coarse_mesh_worker._parser().parse_args(
                [
                    "--config",
                    "config/coarse_mesh.toml",
                    "--characteristic-length",
                    "0.30",
                    "--output",
                    "runs/mesh/coarse/test",
                ]
            )


if __name__ == "__main__":
    unittest.main()
