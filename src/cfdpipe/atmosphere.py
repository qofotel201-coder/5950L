"""SI implementation of the 1976 U.S. Standard Atmosphere.

The project configuration selects this model and declares case altitudes as
geopotential kilometres.  This module contains only model constants; project
altitudes and reference quantities remain in ``config``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


MODEL_NAME = "US_Standard_Atmosphere_1976"
ALTITUDE_KIND = "geopotential"
VISCOSITY_MODEL = "Sutherland"

# U.S. Standard Atmosphere 1976 constants and geopotential layer definition.
_SEA_LEVEL_TEMPERATURE_K = 288.15
_SEA_LEVEL_PRESSURE_PA = 101_325.0
_STANDARD_GRAVITY_M_S2 = 9.80665
_SPECIFIC_GAS_CONSTANT_J_KG_K = 287.05287
_HEAT_CAPACITY_RATIO = 1.4
_SUTHERLAND_BETA_KG_M_S_SQRT_K = 1.458e-6
_SUTHERLAND_TEMPERATURE_K = 110.4
_LAYER_BASE_ALTITUDES_M = (0.0, 11_000.0, 20_000.0, 32_000.0, 47_000.0, 51_000.0, 71_000.0, 84_852.0)
_LAYER_LAPSE_RATES_K_M = (-0.0065, 0.0, 0.0010, 0.0028, 0.0, -0.0028, -0.0020)


class AtmosphereError(ValueError):
    """Raised when an atmosphere request is outside the configured model."""


@dataclass(frozen=True)
class AtmosphereState:
    """Finite SI freestream state at one geopotential altitude."""

    model: str
    altitude_kind: str
    viscosity_model: str
    geopotential_altitude_m: float
    static_temperature_k: float
    static_pressure_pa: float
    density_kg_m3: float
    speed_of_sound_m_s: float
    dynamic_viscosity_pa_s: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_selection(
    *, model: str, altitude_kind: str, viscosity_model: str
) -> None:
    expected = (
        ("model", model, MODEL_NAME),
        ("altitude_kind", altitude_kind, ALTITUDE_KIND),
        ("viscosity_model", viscosity_model, VISCOSITY_MODEL),
    )
    for label, value, required in expected:
        if value != required:
            raise AtmosphereError(
                f"unsupported atmosphere {label} {value!r}; expected {required!r}"
            )


def _layer_base_states() -> tuple[tuple[float, float], ...]:
    states: list[tuple[float, float]] = [
        (_SEA_LEVEL_TEMPERATURE_K, _SEA_LEVEL_PRESSURE_PA)
    ]
    for index, lapse_rate in enumerate(_LAYER_LAPSE_RATES_K_M):
        base_altitude = _LAYER_BASE_ALTITUDES_M[index]
        next_altitude = _LAYER_BASE_ALTITUDES_M[index + 1]
        base_temperature, base_pressure = states[-1]
        delta_altitude = next_altitude - base_altitude
        next_temperature = base_temperature + lapse_rate * delta_altitude
        if lapse_rate == 0.0:
            next_pressure = base_pressure * math.exp(
                -_STANDARD_GRAVITY_M_S2
                * delta_altitude
                / (_SPECIFIC_GAS_CONSTANT_J_KG_K * base_temperature)
            )
        else:
            next_pressure = base_pressure * (
                next_temperature / base_temperature
            ) ** (
                -_STANDARD_GRAVITY_M_S2
                / (_SPECIFIC_GAS_CONSTANT_J_KG_K * lapse_rate)
            )
        states.append((next_temperature, next_pressure))
    return tuple(states)


_LAYER_BASE_STATES = _layer_base_states()


def us_standard_atmosphere_1976(
    geopotential_altitude_m: float,
    *,
    model: str = MODEL_NAME,
    altitude_kind: str = ALTITUDE_KIND,
    viscosity_model: str = VISCOSITY_MODEL,
) -> AtmosphereState:
    """Return the 1976 standard-atmosphere state in SI units.

    The supported range is the standard geopotential layer table from 0 to
    84.852 km.  Geometric-altitude conversion is intentionally not implicit.
    """

    _validate_selection(
        model=model,
        altitude_kind=altitude_kind,
        viscosity_model=viscosity_model,
    )
    if isinstance(geopotential_altitude_m, bool) or not isinstance(
        geopotential_altitude_m, (int, float)
    ):
        raise AtmosphereError("geopotential_altitude_m must be a finite number")
    altitude = float(geopotential_altitude_m)
    if not math.isfinite(altitude):
        raise AtmosphereError("geopotential_altitude_m must be finite")
    if not _LAYER_BASE_ALTITUDES_M[0] <= altitude <= _LAYER_BASE_ALTITUDES_M[-1]:
        raise AtmosphereError(
            "geopotential altitude is outside the supported 0 to 84852 m range"
        )

    layer_index = len(_LAYER_LAPSE_RATES_K_M) - 1
    for candidate in range(len(_LAYER_LAPSE_RATES_K_M)):
        if altitude < _LAYER_BASE_ALTITUDES_M[candidate + 1]:
            layer_index = candidate
            break

    base_altitude = _LAYER_BASE_ALTITUDES_M[layer_index]
    base_temperature, base_pressure = _LAYER_BASE_STATES[layer_index]
    lapse_rate = _LAYER_LAPSE_RATES_K_M[layer_index]
    delta_altitude = altitude - base_altitude
    temperature = base_temperature + lapse_rate * delta_altitude
    if lapse_rate == 0.0:
        pressure = base_pressure * math.exp(
            -_STANDARD_GRAVITY_M_S2
            * delta_altitude
            / (_SPECIFIC_GAS_CONSTANT_J_KG_K * base_temperature)
        )
    else:
        pressure = base_pressure * (temperature / base_temperature) ** (
            -_STANDARD_GRAVITY_M_S2
            / (_SPECIFIC_GAS_CONSTANT_J_KG_K * lapse_rate)
        )
    density = pressure / (_SPECIFIC_GAS_CONSTANT_J_KG_K * temperature)
    speed_of_sound = math.sqrt(
        _HEAT_CAPACITY_RATIO * _SPECIFIC_GAS_CONSTANT_J_KG_K * temperature
    )
    dynamic_viscosity = (
        _SUTHERLAND_BETA_KG_M_S_SQRT_K
        * temperature ** 1.5
        / (temperature + _SUTHERLAND_TEMPERATURE_K)
    )

    values = (temperature, pressure, density, speed_of_sound, dynamic_viscosity)
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise AtmosphereError("standard-atmosphere calculation produced invalid state")
    return AtmosphereState(
        model=model,
        altitude_kind=altitude_kind,
        viscosity_model=viscosity_model,
        geopotential_altitude_m=altitude,
        static_temperature_k=temperature,
        static_pressure_pa=pressure,
        density_kg_m3=density,
        speed_of_sound_m_s=speed_of_sound,
        dynamic_viscosity_pa_s=dynamic_viscosity,
    )
