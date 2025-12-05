# scenario/fixed_daily_meals.py

from simglucose.simulation.scenario import CustomScenario
from datetime import datetime, timedelta

def create_fixed_meal_scenario(start_time: datetime, days: int = 30):
    """
    Generates explicit meals for all days.
    """

    daily_schedule = [
        (7.5, 50),
        (13, 70),
        (18.0, 70)
    ]

    full_scenario = []

    for day in range(days):
        for hour, carbs in daily_schedule:
            t = start_time + timedelta(days=day, hours=hour)
            full_scenario.append((t, carbs))

    full_scenario.sort(key=lambda x: x[0])
    return CustomScenario(start_time=start_time, scenario=full_scenario)
