# main.py
"""Main entry point for T1D simulation."""
import yaml
import pandas as pd
from datetime import datetime
from src.controller import T1DControllerWalsh,Fuzzy,T1DControllerHybridMPC
from src.simulation import SimulationRunner
from src.utils import PatientLogger


def main():
    """Run T1D simulation for configured patients."""
    # Load profile
    with open("config/profiles/adolescent_014.yaml") as f:
        PROFILE = yaml.safe_load(f)
    # Initialize runner
    # runner = SimulationRunner(T1DControllerWalsh, PROFILE, results_dir="results")
    runner = SimulationRunner(Fuzzy, PROFILE, results_dir="results")

    # Patient list
    patients = [f"adolescent#{i:03d}" for i in range(1,11)]
    summary_rows = []
    type = {
        "normal" : ["adolescent#001","adolescent#004","adolescent#005","adolescent#006","adolescent#010"],
        "sensitive": ["adolescent#003","adolescent#009"],
        "resistive": ["adolescent#002","adolescent#007","adolescent#008"]
    }
    # Simulate each patient
    for name in patients:
        if name in type['sensitive']:
            logger = PatientLogger(patient_name=name, save_path="results")
            result = runner.run_patient(name, logger, days=1, animate=False)
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


# insulin-sensitive patients(hypo-prone like #003, #005, #007)

# normal / average patients(#001,#004,#005,#006,#010)
# ================================================================================
#        Patient  TIR_70_180 (%)  <70 (%)  <54 (%)  >180 (%)  >250 (%)  Mean_BG  CV_%  Total_Basal_U  Total_Bolus_U
# adolescent_001            99.6      0.4      0.0       0.0       0.0    117.6  17.4           11.1            6.9
# adolescent_004            90.0      6.7      0.8       3.3       0.0    120.5  27.9           11.2            6.9
# adolescent_005            76.9      9.0      4.2      14.2       3.1    127.7  39.6           12.4            6.9
# adolescent_006            90.8      0.0      0.0       9.2       0.0    138.3  19.5           14.4            6.9
# adolescent_010            95.2      4.4      0.0       0.4       0.0    113.5  26.0            9.3            6.9
# ================================================================================
# insulin-resistant patients (hyper-prone like #002, #008)