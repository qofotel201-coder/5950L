from __future__ import annotations

import math
import unittest

from cfdpipe.atmosphere import AtmosphereError, us_standard_atmosphere_1976


class StandardAtmosphereTests(unittest.TestCase):
    def assertRelativeClose(
        self, actual: float, expected: float, relative_tolerance: float = 2.0e-5
    ) -> None:
        self.assertTrue(math.isfinite(actual))
        self.assertLessEqual(
            abs(actual - expected),
            relative_tolerance * max(abs(expected), 1.0),
        )

    def test_sea_level_reference_state(self) -> None:
        state = us_standard_atmosphere_1976(0.0)

        self.assertRelativeClose(state.static_temperature_k, 288.15, 1.0e-10)
        self.assertRelativeClose(state.static_pressure_pa, 101325.0, 1.0e-10)
        self.assertRelativeClose(state.density_kg_m3, 1.225000, 2.0e-6)
        self.assertRelativeClose(state.speed_of_sound_m_s, 340.294, 2.0e-6)
        self.assertRelativeClose(state.dynamic_viscosity_pa_s, 1.78938e-5, 2.0e-5)

    def test_layer_boundaries_match_ussa_tables(self) -> None:
        references = (
            (11_000.0, 216.65, 22632.1, 0.363918),
            (20_000.0, 216.65, 5474.89, 0.0880349),
            (32_000.0, 228.65, 868.019, 0.0132250),
        )
        for altitude, temperature, pressure, density in references:
            with self.subTest(altitude=altitude):
                state = us_standard_atmosphere_1976(altitude)
                self.assertRelativeClose(state.static_temperature_k, temperature, 1.0e-8)
                self.assertRelativeClose(state.static_pressure_pa, pressure, 4.0e-5)
                self.assertRelativeClose(state.density_kg_m3, density, 4.0e-5)

    def test_design_altitude_21_km_is_finite_si_state(self) -> None:
        state = us_standard_atmosphere_1976(21_000.0)

        self.assertRelativeClose(state.static_temperature_k, 217.65, 1.0e-8)
        self.assertRelativeClose(state.static_pressure_pa, 4677.89, 5.0e-5)
        self.assertRelativeClose(state.density_kg_m3, 0.0748737, 5.0e-5)
        self.assertRelativeClose(state.speed_of_sound_m_s, 295.750, 4.0e-5)
        self.assertRelativeClose(state.dynamic_viscosity_pa_s, 1.42710e-5, 8.0e-5)
        self.assertEqual(state.as_dict()["geopotential_altitude_m"], 21_000.0)

    def test_invalid_altitude_or_model_fails_closed(self) -> None:
        for invalid in (-1.0, 84_852.1, math.nan, math.inf, True, "21000"):
            with self.subTest(invalid=invalid), self.assertRaises(AtmosphereError):
                us_standard_atmosphere_1976(invalid)  # type: ignore[arg-type]
        with self.assertRaisesRegex(AtmosphereError, "model"):
            us_standard_atmosphere_1976(21_000.0, model="invented")
        with self.assertRaisesRegex(AtmosphereError, "altitude_kind"):
            us_standard_atmosphere_1976(21_000.0, altitude_kind="geometric")
        with self.assertRaisesRegex(AtmosphereError, "viscosity_model"):
            us_standard_atmosphere_1976(21_000.0, viscosity_model="constant")


if __name__ == "__main__":
    unittest.main()
