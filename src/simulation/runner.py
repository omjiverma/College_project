# src/simulation/runner.py
"""Simulation runner orchestration."""

from simglucose.simulation.env import T1DSimEnv
from simglucose.sensor.cgm import CGMSensor
from simglucose.actuator.pump import InsulinPump
from simglucose.patient.t1dpatient import T1DPatient
from simglucose.simulation.sim_engine import sim, SimObj
from src.scenarios import create_fixed_meal_scenario
from src.utils.logging import PatientLogger

from datetime import timedelta, datetime
from typing import Type


class SimulationRunner:
    """Orchestrates T1D simulation runs for patients."""

    def __init__(self, controller_class: Type, profile: dict, results_dir: str = "results"):
        """
        Initialize simulation runner.
        
        Args:
            controller_class: Controller class to use
            profile: Configuration profile dictionary
            results_dir: Directory for saving results
        """
        self.controller_class = controller_class
        self.profile = profile
        self.results_dir = results_dir

    def run_patient(self, patient_name: str, logger: PatientLogger = None, 
                   days: int = 7, animate: bool = False):
        """
        Run simulation for a single patient.
        
        Args:
            patient_name: Name of patient (must exist in simglucose database)
            logger: Optional PatientLogger for recording data
            days: Number of days to simulate
            animate: Whether to animate the plot
            
        Returns:
            Simulation result
        """
        # Initialize patient components
        patient = T1DPatient.withName(patient_name)
        sensor = CGMSensor.withName('Dexcom', seed=10)
        pump = InsulinPump.withName('Insulet')
        scenario = create_fixed_meal_scenario(start_time=datetime(2025, 1, 1), days=days)

        # Create environment
        env = T1DSimEnv(patient, sensor, pump, scenario)

        # Initialize controller
        controller = self.controller_class(self.profile, logger=logger)

        # Create simulation object
        sim_obj = SimObj(
            env, 
            controller,
            timedelta(days=days),
            animate=animate,
            path=self.results_dir
        )

        # Run simulation
        return sim(sim_obj)
