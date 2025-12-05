# run_simulation.py
from simglucose.simulation.env import T1DSimEnv
from simglucose.sensor.cgm import CGMSensor
from simglucose.actuator.pump import InsulinPump
from simglucose.patient.t1dpatient import T1DPatient
from simglucose.simulation.scenario_gen import RandomScenario
from simglucose.simulation.sim_engine import sim, SimObj
from utils.logging import PatientLogger
from datetime import timedelta, datetime
import yaml
import pandas as pd
from scenario.fixed_daily_meals import create_fixed_meal_scenario
from simglucose.controller.basal_bolus_ctrller import BBController as BBController
from controller import T1DControllerWalsh as Controller

# Load profile
with open("profiles/adolescent_02.yaml") as f:
    PROFILE = yaml.safe_load(f)

patients = [f"adolescent#{i:03d}" for i in range(2,3)]
animate = False
days = 7
summary_rows = []

for idx, name in enumerate(patients, 1):
    patient = T1DPatient.withName(name)
    sensor  = CGMSensor.withName('Dexcom', seed=10)
    pump    = InsulinPump.withName('Insulet')
    # scenario = RandomScenario(start_time=datetime(2025,1,1), seed=10)
    scenario = create_fixed_meal_scenario(start_time=datetime(2025,1,1), days=days)

    # Create logger for this patient
    logger = PatientLogger(patient_name=name, save_path="results")

    env = T1DSimEnv(patient, sensor, pump, scenario)
    controller = Controller(PROFILE, logger=logger)
    BController = BBController()

    sim_obj = SimObj(
        env, controller,
        timedelta(days=days),
        animate=animate,
        path='results'  # still used by simglucose for its own plots (optional)
    )
    result = sim(sim_obj)

    # Save detailed CSV + compute summary
    logger.save()
    summary_rows.append(logger.get_summary())

# Final summary table
summary_df = pd.DataFrame(summary_rows)
summary_df.to_csv("results/SUMMARY_ALL_PATIENTS.csv", index=False)
print("\n" + "="*80)
print(summary_df.to_string(index=False, float_format=lambda x: f"{x:5.1f}"))
print("="*80)
print(f"All logs saved in ./results/ | Full summary → SUMMARY_ALL_PATIENTS.csv")