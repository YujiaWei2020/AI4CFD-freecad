"""Module for calculating turbulence parameters for CFD simulations.

This module provides functions to calculate turbulent kinetic energy (k),
dissipation rate (epsilon), and specific dissipation rate (omega) based on inlet
velocity and pipe geometry using the k-epsilon model constants.
"""

import math


def calculate_turbulence_parameters(inlet_speed: float,
                                    pipe_radius: float,
                                    turbulence_intensity: float = 0.05,
                                    C_mu: float = 0.09) -> dict[str, float]:
    """Calculates turbulence parameters k, epsilon, and omega.

    Args:
        inlet_speed: Magnitude of inlet velocity in m/s.
        pipe_radius: Radius of the pipe in m.
        turbulence_intensity: Turbulence intensity (I), default 5% (0.05).
        C_mu: Empirical constant for k-epsilon model, default 0.09.

    Returns:
        A dictionary containing calculated values for 'k', 'epsilon', and 'omega'.
    """
    # Hydraulic Diameter D_h = 2 * radius for a pipe
    D_h = 2.0 * pipe_radius

    # Turbulent Kinetic Energy: k = 1.5 * (U * I)^2
    k_val = 1.5 * (inlet_speed * turbulence_intensity)**2

    # Length Scale: L = 0.07 * D_h
    L = 0.07 * D_h

    # Dissipation Rate: epsilon = C_mu^0.75 * k^1.5 / L
    epsilon_val = (C_mu**0.75) * (k_val**1.5) / L

    # Specific Dissipation Rate: omega = k^0.5 / (C_mu^0.25 * L)
    omega_val = (k_val**0.5) / ((C_mu**0.25) * L)

    return {
        "k": k_val,
        "epsilon": epsilon_val,
        "omega": omega_val
    }
