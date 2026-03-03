# experiments/mpc/run_pure_mpc.py
"""Run simglucose simulation using the pure MPC controller."""

from __future__ import annotations

import pandas as pd
import yaml

from src.controller import T1DControllerPureMPC
from src.simulation import SimulationRunner
from src.utils import PatientLogger


def main():
    with open("config/profiles/adolescent_014.yaml") as f:
        profile = yaml.safe_load(f)

    runner = SimulationRunner(T1DControllerPureMPC, profile, results_dir="results")

    patients = [f"adolescent#{i:03d}" for i in range(1, 6)]
    summary_rows = []

    for name in patients:
        logger = PatientLogger(patient_name=name, save_path="results")
        runner.run_patient(name, logger, days=7, animate=False)
        logger.save()
        summary_rows.append(logger.get_summary())

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv("results/SUMMARY_PURE_MPC.csv", index=False)

    print("\n" + "=" * 80)
    print(summary_df.to_string(index=False, float_format=lambda x: f"{x:5.1f}"))
    print("=" * 80)
    print(f"All logs saved in ./results/ | Full summary \u2192 SUMMARY_PURE_MPC.csv")


if __name__ == "__main__":
    main()
