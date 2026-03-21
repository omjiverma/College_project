from simglucose.simulation.scenario_gen import RandomScenario
from datetime import datetime

def create_random_meal_scenario(start_time: datetime, days: int = 30, seed: int = 1):
    """
    Generate random meal schedule scenario.
    
    Args:
        start_time: Simulation start datetime
        days: Number of days to simulate (default 30)
        seed: Random seed for reproducibility (default 1)
        
    Returns:
        RandomScenario object with meals at random times
    """
    return RandomScenario(start_time=start_time, seed=seed)