from __future__ import annotations

import copy
import json
import math
import unittest

from cfdpipe.rans_convergence import (
    LOAD_COMPONENTS,
    MEASUREMENT_SCALARS,
    evaluate_rans_convergence,
)


def _series_tolerance(absolute: float, relative: float = 0.002, **limits):
    return {
        "absolute_tolerance": absolute,
        "relative_tolerance": relative,
        **limits,
    }


def convergence_criteria() -> dict:
    load_absolute = {
        "CFx": 2.0e-4,
        "CFy": 2.0e-4,
        "CFz": 2.0e-6,
        "CMx": 2.0e-6,
        "CMy": 2.0e-4,
        "CMz": 2.0e-4,
    }
    measurement_absolute = {
        "net_mass_flow_kg_s": 0.02,
        "forward_mass_flow_kg_s": 0.02,
        "mass_weighted_mach": 2.0e-3,
        "mass_weighted_static_pressure_pa": 2.0,
        "mass_weighted_static_temperature_k": 0.05,
        "mass_weighted_total_pressure_pa": 5.0,
        "total_pressure_recovery": 2.0e-4,
        "area_m2": 1.0e-8,
        "backflow_mass_fraction": 1.0e-5,
    }
    measurement = {
        name: _series_tolerance(absolute)
        for name, absolute in measurement_absolute.items()
    }
    measurement["forward_mass_flow_kg_s"]["minimum"] = 0.0
    measurement["area_m2"]["minimum"] = 1.0e-12
    measurement["backflow_mass_fraction"]["minimum"] = 0.0
    measurement["backflow_mass_fraction"]["maximum"] = 1.0e-3
    return {
        "maximum_iterations": 12,
        "window_size": 4,
        "required_consecutive_windows": 3,
        "loads": {
            name: _series_tolerance(load_absolute[name])
            for name in LOAD_COMPONENTS
        },
        "measurement": measurement,
        "mass_balance": {"maximum_relative_imbalance": 2.0e-3},
        "outlets": {
            "names": ["rear_outlet_1", "rear_outlet_2"],
            "minimum_normal_mach_margin": 0.25,
            "maximum_subsonic_area_fraction": 0.0,
            "maximum_nonpositive_area_fraction": 0.0,
            "maximum_backflow_area_fraction": 0.0,
            "maximum_reverse_mass_flow_kg_s": 0.0,
        },
        "yplus": {
            "maximum": 1.0,
            "maximum_exceedance_area_fraction": 0.0,
        },
        "residuals": {
            "RMS_DENSITY": {
                "maximum_final": -4.0,
                "minimum_reduction": 2.0,
            },
            "RMS_TKE": {
                "maximum_final": -4.0,
                "minimum_reduction": 2.0,
            },
        },
        "oscillation": {
            "minimum_samples": 12,
            "minimum_cycles": 3,
            "minimum_period_samples": 2,
            "maximum_period_samples": 4,
            "minimum_autocorrelation": 0.8,
            "minimum_amplitude_retention_ratio": 0.75,
        },
    }


def stable_checkpoints(count: int = 12) -> list[dict]:
    load_base = {
        "CFx": 0.40,
        "CFy": 0.10,
        "CFz": 0.0,
        "CMx": 0.0,
        "CMy": 0.02,
        "CMz": -0.03,
    }
    measurement_base = {
        "net_mass_flow_kg_s": 10.0,
        "forward_mass_flow_kg_s": 10.0,
        "mass_weighted_mach": 2.4,
        "mass_weighted_static_pressure_pa": 4700.0,
        "mass_weighted_static_temperature_k": 220.0,
        "mass_weighted_total_pressure_pa": 250000.0,
        "total_pressure_recovery": 0.97,
        "area_m2": 0.011309218033,
        "backflow_mass_fraction": 0.0,
    }
    checkpoints = []
    for index in range(count):
        sign = -1.0 if index % 2 else 1.0
        loads = {
            name: value + sign * (5.0e-7 if abs(value) < 1.0e-12 else 2.0e-6)
            for name, value in load_base.items()
        }
        measurement = {
            name: value + sign * max(abs(value) * 1.0e-7, 1.0e-10)
            for name, value in measurement_base.items()
        }
        measurement["backflow_mass_fraction"] = 0.0
        residual = -2.0 - 0.30 * index
        checkpoints.append(
            {
                "iteration": index + 1,
                "loads": loads,
                "measurement": measurement,
                "mass_balance": {"relative_global_imbalance": 1.0e-4},
                "outlets": {
                    "rear_outlet_1": {
                        "minimum_normal_mach_margin": 0.95,
                        "subsonic_area_fraction": 0.0,
                        "nonpositive_area_fraction": 0.0,
                        "backflow_area_fraction": 0.0,
                        "reverse_mass_flow_kg_s": 0.0,
                    },
                    "rear_outlet_2": {
                        "minimum_normal_mach_margin": 3.4,
                        "subsonic_area_fraction": 0.0,
                        "nonpositive_area_fraction": 0.0,
                        "backflow_area_fraction": 0.0,
                        "reverse_mass_flow_kg_s": 0.0,
                    },
                },
                "yplus": {
                    "all_finite": True,
                    "maximum": 0.80,
                    "above_maximum_area_fraction": 0.0,
                },
                "residuals": {
                    "RMS_DENSITY": residual,
                    "RMS_TKE": residual - 0.1,
                },
            }
        )
    return checkpoints


class RANSConvergenceTests(unittest.TestCase):
    def test_joint_stable_series_converge(self) -> None:
        report = evaluate_rans_convergence(
            stable_checkpoints(), convergence_criteria()
        )

        self.assertEqual(report["status"], "CONVERGED")
        self.assertTrue(report["convergence_claimed"])
        self.assertEqual(report["sample_count"], 12)
        self.assertEqual(report["checks"]["joint_stability"], "PASS")
        self.assertEqual(report["checks"]["hard_gates"], "PASS")
        self.assertFalse(report["oscillation"]["detected"])

    def test_residuals_alone_cannot_override_load_drift(self) -> None:
        checkpoints = stable_checkpoints()
        for index, checkpoint in enumerate(checkpoints):
            checkpoint["loads"]["CFx"] += 0.01 * index

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "MAX_ITER_NOT_CONVERGED")
        self.assertFalse(report["convergence_claimed"])
        self.assertEqual(report["checks"]["residuals"], "PASS")
        self.assertEqual(report["series"]["loads.CFx"]["status"], "FAIL")

    def test_near_zero_components_use_absolute_tolerance(self) -> None:
        checkpoints = stable_checkpoints()
        near_zero = (
            9.0e-7,
            -8.0e-7,
            7.0e-7,
            -6.0e-7,
            5.0e-7,
            -4.0e-7,
            3.0e-7,
            -2.0e-7,
            1.0e-7,
            -1.0e-7,
            2.0e-7,
            -3.0e-7,
        )
        for checkpoint, value in zip(checkpoints, near_zero, strict=True):
            checkpoint["loads"]["CFz"] = value

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "CONVERGED")
        cfz = report["series"]["loads.CFz"]
        self.assertEqual(cfz["status"], "PASS")
        self.assertTrue(all(window["absolute_gate_controls"] for window in cfz["windows"]))

    def test_measurement_drift_prevents_convergence(self) -> None:
        checkpoints = stable_checkpoints()
        for index, checkpoint in enumerate(checkpoints):
            checkpoint["measurement"]["total_pressure_recovery"] += 0.002 * index

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "MAX_ITER_NOT_CONVERGED")
        self.assertEqual(
            report["series"]["measurement.total_pressure_recovery"]["status"],
            "FAIL",
        )

    def test_hard_physics_gates_fail_closed_and_accumulate(self) -> None:
        checkpoints = stable_checkpoints()
        checkpoints[-1]["mass_balance"]["relative_global_imbalance"] = 0.01
        checkpoints[-1]["outlets"]["rear_outlet_1"][
            "minimum_normal_mach_margin"
        ] = 0.1
        checkpoints[-1]["outlets"]["rear_outlet_2"][
            "backflow_area_fraction"
        ] = 0.01
        checkpoints[-1]["yplus"]["maximum"] = 1.2

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["convergence_claimed"])
        codes = {failure["code"] for failure in report["failures"]}
        self.assertTrue(
            {
                "GLOBAL_MASS_IMBALANCE",
                "OUTLET_MACH_MARGIN",
                "OUTLET_BACKFLOW",
                "YPLUS_MAXIMUM",
            }.issubset(codes)
        )

    def test_nonfinite_data_is_fail_not_a_convergence_result(self) -> None:
        checkpoints = stable_checkpoints()
        checkpoints[-1]["measurement"]["mass_weighted_mach"] = math.nan

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failures"][0]["code"], "DATA_ERROR")
        self.assertFalse(report["convergence_claimed"])

    def test_sustained_detrended_periodic_signal_requires_urans_review(self) -> None:
        checkpoints = stable_checkpoints()
        waveform = (0.0, 0.02, 0.0, -0.02)
        for index, checkpoint in enumerate(checkpoints):
            checkpoint["loads"]["CFx"] = 0.4 + waveform[index % 4]

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertEqual(report["status"], "URANS_REVIEW_REQUIRED")
        self.assertFalse(report["convergence_claimed"])
        self.assertTrue(report["oscillation"]["detected"])
        self.assertIn("loads.CFx", report["oscillation"]["detected_signals"])
        evidence = report["oscillation"]["signals"]["loads.CFx"]
        self.assertEqual(evidence["status"], "PERSISTENT_PERIODIC")
        self.assertEqual(evidence["best_period_samples"], 4)

    def test_damped_oscillation_is_not_misclassified_as_persistent(self) -> None:
        checkpoints = stable_checkpoints()
        for index, checkpoint in enumerate(checkpoints):
            amplitude = 0.03 * (0.72**index)
            checkpoint["loads"]["CFx"] = 0.4 + amplitude * math.sin(
                0.5 * math.pi * index
            )

        report = evaluate_rans_convergence(checkpoints, convergence_criteria())

        self.assertNotEqual(report["status"], "URANS_REVIEW_REQUIRED")
        self.assertFalse(report["oscillation"]["detected"])

    def test_insufficient_windows_at_iteration_limit_are_not_converged(self) -> None:
        criteria = convergence_criteria()
        criteria["maximum_iterations"] = 8
        checkpoints = stable_checkpoints(8)

        report = evaluate_rans_convergence(checkpoints, criteria)

        self.assertEqual(report["status"], "MAX_ITER_NOT_CONVERGED")
        self.assertEqual(report["checks"]["joint_stability"], "INSUFFICIENT_DATA")
        self.assertFalse(report["convergence_claimed"])

    def test_convergence_requires_enough_samples_for_oscillation_gate(self) -> None:
        criteria = convergence_criteria()
        criteria["window_size"] = 2
        criteria["maximum_iterations"] = 6

        report = evaluate_rans_convergence(stable_checkpoints(6), criteria)

        self.assertEqual(report["checks"]["joint_stability"], "PASS")
        self.assertEqual(report["checks"]["oscillation"], "INSUFFICIENT_DATA")
        self.assertEqual(report["status"], "MAX_ITER_NOT_CONVERGED")
        self.assertFalse(report["convergence_claimed"])

    def test_missing_six_component_load_or_threshold_is_fail_closed(self) -> None:
        checkpoints = stable_checkpoints()
        del checkpoints[-1]["loads"]["CMz"]
        report = evaluate_rans_convergence(checkpoints, convergence_criteria())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failures"][0]["code"], "DATA_ERROR")

        criteria = convergence_criteria()
        del criteria["measurement"][MEASUREMENT_SCALARS[-1]]
        report = evaluate_rans_convergence(stable_checkpoints(), criteria)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failures"][0]["code"], "CONFIG_ERROR")

    def test_run_stopping_early_without_convergence_is_fail(self) -> None:
        criteria = convergence_criteria()
        criteria["maximum_iterations"] = 20
        checkpoints = stable_checkpoints()
        for index, checkpoint in enumerate(checkpoints):
            checkpoint["loads"]["CFy"] += 0.005 * index

        report = evaluate_rans_convergence(checkpoints, criteria)

        self.assertEqual(report["status"], "FAIL")
        self.assertIn(
            "RUN_ENDED_BEFORE_CONVERGENCE_OR_LIMIT",
            {failure["code"] for failure in report["failures"]},
        )

    def test_evaluator_does_not_mutate_inputs(self) -> None:
        criteria = convergence_criteria()
        checkpoints = stable_checkpoints()
        expected_criteria = copy.deepcopy(criteria)
        expected_checkpoints = copy.deepcopy(checkpoints)

        evaluate_rans_convergence(checkpoints, criteria)

        self.assertEqual(criteria, expected_criteria)
        self.assertEqual(checkpoints, expected_checkpoints)

    def test_report_is_strict_json_serializable(self) -> None:
        report = evaluate_rans_convergence(
            stable_checkpoints(), convergence_criteria()
        )

        encoded = json.dumps(report, allow_nan=False, sort_keys=True)
        self.assertIn('"status": "CONVERGED"', encoded)


if __name__ == "__main__":
    unittest.main()
