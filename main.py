import pandas as pd
from src.controller import Fuzzy
from src.controller.pid_controller import PID
from src.controller.esp32_controller import ESP32_Controller
from src.simulation import SimulationRunner
from src.utils import PatientLogger


def main():
    """Run T1D simulation for configured patients."""
    # Load profile
    # Initialize runner
    # runner = SimulationRunner(T1DControllerWalsh, PROFILE, results_dir="results")
 
    runner = SimulationRunner(PID, results_dir="results")
    # runner = SimulationRunner(Fuzzy, results_dir="results")

    # Patient list
    patients = [f"adolescent#{i:03d}" for i in range(1,11)]
    summary_rows = []
    patients_types = {
        "NORMAL_ADOLESCENT": ["adolescent#001","adolescent#004","adolescent#005","adolescent#006","adolescent#010"],
        "SENSITIVE_ADOLESCENT": ["adolescent#003","adolescent#009",],
        "RESISTANT_ADOLESCENT": ["adolescent#002","adolescent#007","adolescent#008"],
        "NORMAL_ADULT": ["adult#001", "adult#002","adult#003", "adult#008"],
        "SENSITIVE_ADULT": ["adult#004", "adult#007"],
        "RESISTANT_ADULT": ["adult#005", "adult#006", "adult#009", "adult#010"]
    }
    # Simulate each patient
    for name in patients:
        if name in patients_types['SENSITIVE_ADOLESCENT']:
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
