# src/utils/logging.py
"""Logging utilities for T1D simulation."""

import os
import pandas as pd
from datetime import datetime


class PatientLogger:
    """Logger for recording T1D simulation data."""

    def __init__(self, patient_name: str, save_path: str = "results"):
        """
        Initialize patient logger.
        
        Args:
            patient_name: Name of the patient
            save_path: Directory to save logs (default "results")
        """
        self.patient_name = patient_name.replace("#", "_")
        os.makedirs(save_path, exist_ok=True)

        self.log_path = os.path.join(save_path, f"{self.patient_name}.csv")
        self.data = []
        self.start_time = None

    def log_step(self, step, time, cgm, basal, bolus, iob, iob_model,
                 cho, aggression, trend, target, **extra):
        """
        Log a simulation step.
        
        Args:
            step: Step number
            time: Datetime of step
            cgm: CGM glucose reading (mg/dL)
            basal: Basal rate delivered (U/hr)
            bolus: Bolus delivered (U)
            iob: Insulin on board (U)
            iob_model: IOB model name
            cho: Carbs informed (g)
            aggression: Aggression factor (0-1)
            trend: Glucose trend (mg/dL/min)
            target: Target glucose (mg/dL)
            **extra: Additional fields to log
        """
        if self.start_time is None:
            self.start_time = time

        row = {
            "step": step,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "minutes_from_start": (time - self.start_time).total_seconds()/60,
            "CGM": round(cgm, 1),
            "basal_rate": round(basal, 5),
            "bolus": round(bolus, 4),
            "IOB": round(iob, 3),
            "IOB_Model": iob_model.upper(),
            "CHO": cho,
            "aggression": round(aggression, 3),
            "trend_mgdl_min": round(trend, 3),
            "target": target,
            **extra
        }

        self.data.append(row)

    def save(self):
        """Save logged data to CSV file."""
        if not self.data:
            return
        df = pd.DataFrame(self.data)
        df.to_csv(self.log_path, index=False)
        # Avoid UnicodeEncodeError on Windows consoles with non-UTF8 code pages.
        print(f"Saved log -> {self.log_path}")

    def get_summary(self) -> dict:
        """
        Compute summary statistics from logged data.
        
        Returns:
            Dictionary with summary metrics
        """
        if not self.data:
            return {}

        df = pd.DataFrame(self.data)
        bg = df["CGM"]

        return {
            "Patient": self.patient_name,
            "TIR_70_180 (%)": round(((bg>=70)&(bg<=180)).mean()*100, 1),
            "<70 (%)": round((bg<70).mean()*100, 2),
            "<54 (%)": round((bg<54).mean()*100, 2),
            ">180 (%)": round((bg>180).mean()*100, 1),
            ">250 (%)": round((bg>250).mean()*100, 2),
            "Mean_BG": round(bg.mean(), 1),
            "CV_%": round(bg.std()/bg.mean()*100, 1),
            "Total_Basal_U": round(df["basal_rate"].sum()*5/60, 3),
            "Total_Bolus_U": round(df["bolus"].sum(), 3),
        }
