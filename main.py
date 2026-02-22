# main.py
"""Main entry point for T1D simulation."""

import yaml
import pandas as pd
from datetime import datetime
from src.controller import T1DControllerWalsh
from src.simulation import SimulationRunner
from src.utils import PatientLogger


def main():
    """Run T1D simulation for configured patients."""
    # Load profile
    with open("config/profiles/adolescent_02.yaml") as f:
        PROFILE = yaml.safe_load(f)

    # Initialize runner
    runner = SimulationRunner(T1DControllerWalsh, PROFILE, results_dir="results")

    # Patient list
    patients = [f"adolescent#{i:03d}" for i in range(2, 3)]
    summary_rows = []

    # Simulate each patient
    for name in patients:
        logger = PatientLogger(patient_name=name, save_path="results")
        result = runner.run_patient(name, logger, days=7, animate=False)
        logger.save()
        summary_rows.append(logger.get_summary())

    # Save summary statistics
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv("results/SUMMARY_ALL_PATIENTS.csv", index=False)
    
    # Print results
    print("\n" + "="*80)
    print(summary_df.to_string(index=False, float_format=lambda x: f"{x:5.1f}"))
    print("="*80)
    print(f"All logs saved in ./results/ | Full summary → SUMMARY_ALL_PATIENTS.csv")


if __name__ == "__main__":
    main()
