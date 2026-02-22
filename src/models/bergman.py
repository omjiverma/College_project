# src/models/bergman.py
"""Bergman Minimal Model for glucose-insulin dynamics."""

import numpy as np
from typing import Tuple


class BergmanMinimalModel:
    """
    Bergman Minimal Model for T1D glucose-insulin dynamics.
    Uses Euler method for discrete simulation.
    """

    def __init__(self, profile: dict):
        """
        Initialize Bergman model parameters from profile.
        
        Args:
            profile: Dictionary containing model parameters
        """
        self.p1 = profile.get("p1", 0.028)
        self.p2 = profile.get("p2", 0.025)
        self.p3 = profile.get("p3", 5e-5)
        self.n = profile.get("n", 0.05)
        self.Gb = profile.get("Gb", 110.0)
        self.Ib = profile.get("Ib", 15.0)
        self.alpha = profile.get("alpha", 300.0)
        
        self.X_state = 0.0
        self.I_state = float(self.Ib)

    def step(self, G: float, X: float, I: float, U: float, D: float = 0.0, dt: float = 3.0) -> Tuple[float, float, float]:
        """
        One discrete Bergman step using Euler integration.
        
        Args:
            G: Glucose (mg/dL)
            X: Insulin action (1/min)
            I: Plasma insulin concentration (μU/mL)
            U: Exogenous insulin delivered (U)
            D: Meal appearance rate (mg/dL/min), default 0
            dt: Time step in minutes, default 3.0
            
        Returns:
            Tuple of (G_new, X_new, I_new)
        """
        # Differential equations
        dG = -(self.p1 + X) * G + self.p1 * self.Gb + D
        dX = -self.p2 * X + self.p3 * (I - self.Ib)
        dI = -self.n * (I - self.Ib) + self.alpha * U

        # Euler step
        G_new = G + dt * dG
        X_new = X + dt * dX
        I_new = I + dt * dI

        return float(G_new), float(X_new), float(I_new)

    def reset(self):
        """Reset internal states to initial values."""
        self.X_state = 0.0
        self.I_state = float(self.Ib)

    def update_states(self, X: float, I: float):
        """Update internal state variables."""
        self.X_state = float(X)
        self.I_state = float(I)

    def get_states(self) -> Tuple[float, float]:
        """Get current internal states."""
        return self.X_state, self.I_state
