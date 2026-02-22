# src/scenarios/fixed_meals.py
"""Fixed meal scenario generation."""

from simglucose.simulation.scenario import CustomScenario
from datetime import datetime, timedelta


def create_fixed_meal_scenario(start_time: datetime, days: int = 30):
    """
    Generate fixed meal schedule scenario.
    
    Args:
        start_time: Simulation start datetime
        days: Number of days to simulate (default 30)
        
    Returns:
        CustomScenario object with meals at fixed times
    """
    daily_schedule = [
        (7.5, 50),      # Breakfast at 7:30 AM - 50g carbs
        (13, 70),       # Lunch at 1:00 PM - 70g carbs
        (18.0, 70)      # Dinner at 6:00 PM - 70g carbs
    ]

    full_scenario = []

    for day in range(days):
        for hour, carbs in daily_schedule:
            t = start_time + timedelta(days=day, hours=hour)
            full_scenario.append((t, carbs))

    full_scenario.sort(key=lambda x: x[0])
    return CustomScenario(start_time=start_time, scenario=full_scenario)
