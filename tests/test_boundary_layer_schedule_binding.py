from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

from cfdpipe.boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
    load_and_bind_boundary_layer_local_schedule,
    validate_boundary_layer_schedule_binding,
)
from cfdpipe.boundary_layer_smoke import (
    BoundaryLayerSmokeError,
    _assign_root_local_schedules,
    _validated_local_surface_schedules,
)
from cfdpipe.coarse_mesh import (
    CoarseMeshError,
    make_coarse_strategy_config,
    normalize_coarse_mesh_config,
)
from cfdpipe.coarse_mesh_projection import make_projection_strategy_config
from cfdpipe.coarse_repair_audit import make_coarse_repair_audit_strategy_config


ROOT = Path(__file__).resolve().parents[1]


def _canonical(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _contract() -> dict:
    document = tomllib.loads((ROOT / "config" / "coarse_mesh.toml").read_text("utf-8"))
    return normalize_coarse_mesh_config(document, repository_root=ROOT)


def _stack(first: float, count: int, growth: float) -> list[float]:
    values = []
    running = 0.0
    for layer in range(count):
        running += first * growth**layer
        values.append(running)
    return values


def _plan(contract: dict) -> dict:
    design = contract["boundary_layer_design"]
    first = float(design["first_layer_height_m"])
    count = int(design["layer_count"])
    maximum_growth = float(design["growth_ratio"])
    global_total = float(design["total_thickness_m"])
    fraction = float(contract["boundary_layer_maximum_clearance_fraction"])
    core_ratio = float(design["maximum_core_to_last_layer_ratio"])
    clearance_sha = "c" * 64
    schedules = []
    for index, fingerprint in enumerate(contract["wall_surface_fingerprints"]):
        growth = maximum_growth if index else 1.15
        cumulative = _stack(first, count, growth)
        total = cumulative[-1]
        clearance = total / fraction
        last = cumulative[-1] - cumulative[-2]
        record = {
            "surface_fingerprint_id": fingerprint,
            "status": "PASS",
            "layer_count": count,
            "first_layer_height_m": first,
            "maximum_growth_ratio": maximum_growth,
            "global_design_total_thickness_m": global_total,
            "minimum_clearance_lower_bound_m": clearance,
            "maximum_clearance_fraction": fraction,
            "clearance_limited_target_total_thickness_m": total,
            "minimum_fixed_stack_total_thickness_m": first * count,
            "surface_clearance_evidence_sha256": f"{index + 1:064x}",
            "clearance_report_sha256": clearance_sha,
            "coarse_contract_sha256": contract["normalized_config_sha256"],
            "failure_reason": None,
            "local_growth_ratio": growth,
            "total_thickness_m": total,
            "last_layer_height_m": last,
            "maximum_core_to_last_layer_ratio": core_ratio,
            "recommended_local_core_max_m": last * core_ratio,
            "clearance_margin_m": clearance - total,
            "two_sided_core_clearance_margin_m": clearance - 2.0 * total,
        }
        record["schedule_evidence_sha256"] = _canonical(record)
        schedules.append(record)
    return {
        "schema": "cfdpipe.boundary_layer_local_schedule.v1",
        "status": "PASS",
        "planning_only": True,
        "extrusion_authorized": False,
        "extrusion_feasibility_claimed": False,
        "requires_builder_integration_and_mesh_quality_gate": True,
        "mesh_generated": False,
        "su2_called": False,
        "paraview_called": False,
        "production_mesh_eligible": False,
        "coarse_contract_sha256": contract["normalized_config_sha256"],
        "clearance_evidence_contract_sha256": contract[
            "clearance_evidence_contract_sha256"
        ],
        "clearance_report": {
            "path": str(ROOT / "runs" / "mock-clearance.json"),
            "sha256": clearance_sha,
            "size_bytes": 1,
            "unchanged_during_planning": True,
            "execution_status": "PASS",
            "coarse_contract_sha256": contract["clearance_evidence_contract_sha256"],
            "contract_binding": "preserved_pre_local_policy_clearance_contract",
            "uniform_clearance_requirement_status": "FAIL",
        },
        "source_geometry": {
            "path": contract["pipeline_brep_path"],
            "sha256": contract["provenance"]["pipeline_brep_sha256"],
            "unchanged_in_clearance_audit": True,
            "gmsh_finalize_called": True,
        },
        "policy": {
            "first_layer_height_m": first,
            "layer_count": count,
            "minimum_layer_count": int(design["project_minimum_layer_count"]),
            "maximum_growth_ratio": maximum_growth,
            "global_design_total_thickness_m": global_total,
            "maximum_clearance_fraction": fraction,
            "maximum_core_to_last_layer_ratio": core_ratio,
            "first_layer_height_reduced": False,
            "layer_count_reduced": False,
        },
        "surface_count": 48,
        "sample_count": 240,
        "pass_surface_count": 48,
        "fail_surface_count": 0,
        "schedules": schedules,
        "failure_reasons": [],
    }


def _write_plan(directory: Path, plan: dict) -> tuple[Path, str]:
    path = directory / "boundary_layer_local_schedule.json"
    payload = (json.dumps(plan, indent=2, allow_nan=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _rehash(document: dict, field: str) -> None:
    document.pop(field, None)
    document[field] = _canonical(document)


class LocalScheduleBindingTests(unittest.TestCase):
    def test_audit_only_mode_consumes_schedule_only_with_complete_safety_scope(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract,
                plan_path=path,
                plan_sha256=digest,
            )
            audit = make_coarse_repair_audit_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=binding,
            )
            schedules, evidence = _validated_local_surface_schedules(audit)
            self.assertEqual(48, len(schedules))
            self.assertEqual(binding["binding_sha256"], evidence["binding_sha256"])

            required_scope = {
                "smoke_only": False,
                "calibration_only": False,
                "repair_audit_only": True,
                "projection_only": False,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "su2_called": False,
                "paraview_called": False,
            }
            for field, required in required_scope.items():
                with self.subTest(field=field):
                    unsafe = copy.deepcopy(audit)
                    unsafe[field] = not required
                    unsafe.pop("normalized_config_sha256", None)
                    unsafe["normalized_config_sha256"] = _canonical(unsafe)
                    with self.assertRaisesRegex(
                        BoundaryLayerSmokeError, "audit-only repair safety scope"
                    ):
                        _validated_local_surface_schedules(unsafe)

    def test_minimum_growth_endpoint_is_applied_only_in_repair_audit_memory(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract,
                plan_path=path,
                plan_sha256=digest,
            )
            binding_before = copy.deepcopy(binding)
            audit = make_coarse_repair_audit_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=binding,
                schedule_feasibility_endpoint=(
                    "minimum-growth-existing-direction-repair"
                ),
            )
            schedules, evidence = _validated_local_surface_schedules(audit)

            first = float(
                contract["boundary_layer_design"]["first_layer_height_m"]
            )
            layers = int(contract["boundary_layer_design"]["layer_count"])
            expected = [first * index for index in range(1, layers + 1)]
            self.assertEqual(48, len(schedules))
            self.assertTrue(
                all(values == expected for values in schedules.values())
            )
            self.assertEqual(binding_before, binding)
            self.assertEqual(
                "PASS",
                evidence["schedule_feasibility_application"]["status"],
            )
            self.assertFalse(
                evidence["schedule_feasibility_application"][
                    "calibration_PASS_authorized"
                ]
            )
            self.assertAlmostEqual(
                first * layers,
                evidence["maximum_surface_total_thickness_m"],
            )

            for field, replacement in (
                ("audit_purpose", "repair_profile_discovery"),
                ("combined_endpoint_only", False),
            ):
                with self.subTest(field=field):
                    tampered = copy.deepcopy(audit)
                    tampered[field] = replacement
                    _rehash(tampered, "normalized_config_sha256")
                    with self.assertRaisesRegex(
                        BoundaryLayerSmokeError, "purpose/scope"
                    ):
                        _validated_local_surface_schedules(tampered)

            tampered_endpoint = copy.deepcopy(audit)
            tampered_endpoint["schedule_feasibility_endpoint"][
                "candidate_growth_ratio"
            ] = 1.01
            _rehash(tampered_endpoint, "normalized_config_sha256")
            with self.assertRaisesRegex(
                BoundaryLayerSmokeError, "schedule feasibility endpoint"
            ):
                _validated_local_surface_schedules(tampered_endpoint)

    def test_hash_bound_pass_plan_drives_calibration_and_keeps_smoke_construction_height(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract,
                plan_path=path,
                plan_sha256=digest,
            )
            strategy = make_coarse_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=binding,
            )
            self.assertEqual(binding["status"], "PASS")
            self.assertEqual(binding["surface_count"], 48)
            self.assertEqual(binding["layer_count"], 60)
            self.assertEqual(
                strategy["construction_first_layer_height_m"],
                contract["base_smoke_contract"]["construction_first_layer_height_m"],
            )
            self.assertEqual(
                strategy["local_boundary_layer_schedule"]["binding_sha256"],
                binding["binding_sha256"],
            )
            schedules, evidence = _validated_local_surface_schedules(strategy)
            self.assertEqual(len(schedules), 48)
            self.assertEqual(evidence["binding_sha256"], binding["binding_sha256"])

    def test_calibration_fails_without_plan_and_projection_needs_none(self) -> None:
        contract = _contract()
        first_point = contract["calibration_characteristic_lengths_m"][0]
        with self.assertRaisesRegex(CoarseMeshError, "hash-bound local schedule"):
            make_coarse_strategy_config(contract, first_point)
        projection = make_projection_strategy_config(contract, first_point)
        self.assertTrue(projection["projection_only"])
        self.assertNotIn("local_boundary_layer_schedule", projection)
        self.assertEqual(
            projection["construction_first_layer_height_m"],
            contract["base_smoke_contract"]["construction_first_layer_height_m"],
        )

    def test_stale_file_tamper_and_missing_surface_fail_closed(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, digest = _write_plan(root, _plan(contract))
            path.write_text(path.read_text("utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(BoundaryLayerScheduleBindingError, "stale"):
                load_and_bind_boundary_layer_local_schedule(
                    coarse_contract=contract,
                    plan_path=path,
                    plan_sha256=digest,
                )

            missing = _plan(contract)
            missing["schedules"].pop()
            missing["surface_count"] = 47
            missing["pass_surface_count"] = 47
            other, other_hash = _write_plan(root, missing)
            with self.assertRaisesRegex(
                BoundaryLayerScheduleBindingError, "48 PASS surfaces"
            ):
                load_and_bind_boundary_layer_local_schedule(
                    coarse_contract=contract,
                    plan_path=other,
                    plan_sha256=other_hash,
                )

    def test_binding_and_surface_record_tampering_are_rejected_at_consumption(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract, plan_path=path, plan_sha256=digest
            )
        tampered = copy.deepcopy(binding)
        fingerprint = next(iter(tampered["surface_schedules"]))
        tampered["surface_schedules"][fingerprint]["cumulative_heights_m"][-1] *= 2.0
        with self.assertRaisesRegex(CoarseMeshError, "binding hash is stale"):
            make_coarse_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=tampered,
            )

    def test_authoritative_revalidation_rejects_a_self_rehashed_binding(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract, plan_path=path, plan_sha256=digest
            )
            tampered = copy.deepcopy(binding)
            fingerprint = next(iter(tampered["surface_schedules"]))
            record = tampered["surface_schedules"][fingerprint]
            growth = float(record["local_growth_ratio"]) - 0.01
            cumulative = _stack(
                float(record["first_layer_height_m"]),
                int(record["layer_count"]),
                growth,
            )
            record["local_growth_ratio"] = growth
            record["cumulative_heights_m"] = cumulative
            record["total_thickness_m"] = cumulative[-1]
            record["last_layer_height_m"] = cumulative[-1] - cumulative[-2]
            totals = [
                float(value["total_thickness_m"])
                for value in tampered["surface_schedules"].values()
            ]
            tampered["minimum_total_thickness_m"] = min(totals)
            tampered["maximum_total_thickness_m"] = max(totals)
            _rehash(tampered, "binding_sha256")
            with self.assertRaisesRegex(
                BoundaryLayerScheduleBindingError, "authoritative source plan"
            ):
                validate_boundary_layer_schedule_binding(contract, tampered)

    def test_final_consumer_rejects_lineage_scope_and_source_tampering(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract, plan_path=path, plan_sha256=digest
            )
            audit = make_coarse_repair_audit_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=binding,
            )

            wrong_contract = copy.deepcopy(audit)
            wrong_binding = wrong_contract["local_boundary_layer_schedule"]
            wrong_binding["coarse_contract_sha256"] = "f" * 64
            _rehash(wrong_binding, "binding_sha256")
            _rehash(wrong_contract, "normalized_config_sha256")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "lineage"):
                _validated_local_surface_schedules(wrong_contract)

            for field in (
                "runtime_entity_tags_present",
                "su2_called",
                "paraview_called",
            ):
                with self.subTest(field=field):
                    unsafe = copy.deepcopy(audit)
                    unsafe_binding = unsafe["local_boundary_layer_schedule"]
                    unsafe_binding[field] = True
                    _rehash(unsafe_binding, "binding_sha256")
                    _rehash(unsafe, "normalized_config_sha256")
                    with self.assertRaisesRegex(
                        BoundaryLayerSmokeError, "scope|lineage"
                    ):
                        _validated_local_surface_schedules(unsafe)

            stale = copy.deepcopy(audit)
            path.write_text(path.read_text("utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(BoundaryLayerSmokeError, "stale"):
                _validated_local_surface_schedules(stale)

    def test_final_consumer_rejects_casefold_collision_and_nonphysical_stack(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            path, digest = _write_plan(Path(raw), _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract, plan_path=path, plan_sha256=digest
            )
            audit = make_coarse_repair_audit_strategy_config(
                contract,
                contract["calibration_characteristic_lengths_m"][0],
                local_schedule_binding=binding,
            )

            collision = copy.deepcopy(audit)
            collision_binding = collision["local_boundary_layer_schedule"]
            fingerprint = next(iter(collision_binding["surface_schedules"]))
            uppercase = fingerprint.upper()
            self.assertNotEqual(fingerprint, uppercase)
            duplicate = copy.deepcopy(
                collision_binding["surface_schedules"][fingerprint]
            )
            duplicate["surface_fingerprint_id"] = uppercase
            collision_binding["surface_schedules"][uppercase] = duplicate
            _rehash(collision_binding, "binding_sha256")
            _rehash(collision, "normalized_config_sha256")
            with self.assertRaisesRegex(
                BoundaryLayerSmokeError, "noncanonical, or duplicated"
            ):
                _validated_local_surface_schedules(collision)

            nonphysical = copy.deepcopy(audit)
            nonphysical_binding = nonphysical["local_boundary_layer_schedule"]
            fingerprint = next(iter(nonphysical_binding["surface_schedules"]))
            cumulative = nonphysical_binding["surface_schedules"][fingerprint][
                "cumulative_heights_m"
            ]
            cumulative[1] = (cumulative[0] + cumulative[2]) / 2.0
            _rehash(nonphysical_binding, "binding_sha256")
            _rehash(nonphysical, "normalized_config_sha256")
            with self.assertRaisesRegex(
                BoundaryLayerSmokeError, "physical plan"
            ):
                _validated_local_surface_schedules(nonphysical)

    def test_schedule_path_rejects_symlink_and_junction_but_accepts_normal_absolute_path(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, digest = _write_plan(root, _plan(contract))
            binding = load_and_bind_boundary_layer_local_schedule(
                coarse_contract=contract,
                plan_path=path.resolve(),
                plan_sha256=digest,
            )
            self.assertEqual(str(path.resolve()), binding["source_plan_path"])

            link = root / "schedule-link.json"
            try:
                link.symlink_to(path)
            except OSError:
                link = None
            if link is not None:
                with self.assertRaisesRegex(
                    BoundaryLayerScheduleBindingError, "symbolic link"
                ):
                    load_and_bind_boundary_layer_local_schedule(
                        coarse_contract=contract,
                        plan_path=link,
                        plan_sha256=digest,
                    )

            original_is_junction = Path.is_junction

            def pretend_root_is_junction(candidate: Path) -> bool:
                if candidate == root:
                    return True
                return original_is_junction(candidate)

            with mock.patch.object(
                Path, "is_junction", new=pretend_root_is_junction
            ):
                with self.assertRaisesRegex(
                    BoundaryLayerScheduleBindingError, "junction"
                ):
                    load_and_bind_boundary_layer_local_schedule(
                        coarse_contract=contract,
                        plan_path=path,
                        plan_sha256=digest,
                    )

    def test_shared_root_takes_elementwise_minimum_without_amplification(self) -> None:
        first = 1.0e-6
        schedule_a = _stack(first, 15, 1.2)
        schedule_b = _stack(first, 15, 1.1)
        roots, evidence = _assign_root_local_schedules(
            [
                {"entity": 10, "base_face": (1, 2, 3)},
                {"entity": 20, "base_face": (2, 3, 4)},
            ],
            prism_volume_fingerprints={10: "a" * 64, 20: "b" * 64},
            surface_schedules={"a" * 64: schedule_a, "b" * 64: schedule_b},
            root_coordinates={
                1: (0.0, 0.0, 0.0),
                2: (1.0, 0.0, 0.0),
                3: (0.0, 1.0, 0.0),
                4: (1.0, 1.0, 0.0),
            },
        )
        self.assertEqual(roots[1], schedule_a)
        self.assertEqual(roots[4], schedule_b)
        self.assertEqual(roots[2], [min(a, b) for a, b in zip(schedule_a, schedule_b)])
        self.assertEqual(roots[3], roots[2])
        self.assertEqual(evidence["shared_root_count"], 2)
        self.assertEqual(evidence["root_count"], 4)
        self.assertGreater(evidence["clearance_limited_root_count"], 0)

    def test_missing_generated_surface_and_mismatched_first_height_fail(self) -> None:
        schedule = _stack(1.0e-6, 15, 1.1)
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "generated no source prisms"):
            _assign_root_local_schedules(
                [{"entity": 10, "base_face": (1, 2, 3)}],
                prism_volume_fingerprints={10: "a" * 64},
                surface_schedules={"a" * 64: schedule, "b" * 64: schedule},
            )
        other = _stack(2.0e-6, 15, 1.1)
        with self.assertRaisesRegex(BoundaryLayerSmokeError, "first height"):
            _assign_root_local_schedules(
                [
                    {"entity": 10, "base_face": (1, 2, 3)},
                    {"entity": 20, "base_face": (2, 3, 4)},
                ],
                prism_volume_fingerprints={10: "a" * 64, 20: "b" * 64},
                surface_schedules={"a" * 64: schedule, "b" * 64: other},
            )

    def test_binding_path_has_no_shell_or_downstream_execution(self) -> None:
        modules = (
            __import__("cfdpipe.boundary_layer_schedule_binding", fromlist=["x"]),
            __import__("cfdpipe.coarse_mesh_projection", fromlist=["x"]),
        )
        source = "\n".join(inspect.getsource(module) for module in modules)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("SU2_CFD", source)
        self.assertNotIn("pvbatch", source.casefold())


if __name__ == "__main__":
    unittest.main()
