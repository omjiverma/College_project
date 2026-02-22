# src/models/iob.py
"""Walsh IOB (Insulin on Board) model."""

import numpy as np
from typing import List


class WalshIOB:
    """
    Walsh IOB model for tracking insulin on board.
    Uses exponential decay model for insulin activity.
    """

    def __init__(self, dia_minutes: float = 300.0):
        """
        Initialize IOB model.
        
        Args:
            dia_minutes: Duration of insulin action in minutes (default 300)
        """
        self.DIA = float(dia_minutes)
        self.insulin_history: List[List[float]] = []  # [age_min, U]

    def calculate(self) -> float:
        """
        Calculate IOB using Walsh exponential model.
        
        Returns:
            IOB in units (U)
        """
        if not self.insulin_history:
            return 0.0
        
        iob = 0.0
        for age, u in self.insulin_history:
            age_min = max(age, 0.0)
            
            # Walsh exponential decay
            if age_min <= 180:
                frac = 1.0 - 0.5 * (age_min / 180.0) ** 2
            else:
                frac = 0.5 * np.exp(-(age_min - 180.0) / 120.0)
            
            iob += u * frac
        
        return max(iob, 0.0)

    def update(self, basal_u: float, bolus_u: float, dt_min: float):
        """
        Update IOB with new insulin delivery and age existing entries.
        
        Args:
            basal_u: Basal insulin delivered (U)
            bolus_u: Bolus insulin delivered (U)
            dt_min: Time step in minutes
        """
        # Add new insulin entry if significant
        total = basal_u + bolus_u
        if total > 1e-8:
            self.insulin_history.append([0.0, total])
        
        # Age all existing entries
        for pkt in self.insulin_history:
            pkt[0] += dt_min
        
        # Remove expired insulin (beyond DIA)
        max_age = self.DIA + 180
        self.insulin_history = [pkt for pkt in self.insulin_history if pkt[0] < max_age]

    def reset(self):
        """Clear insulin history."""
        self.insulin_history.clear()

    def get_history(self) -> List[List[float]]:
        """Get current insulin history."""
        return [pkt.copy() for pkt in self.insulin_history]
