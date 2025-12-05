from simglucose.controller.base import Controller, Action
from utils.logging import PatientLogger

import numpy as np
from datetime import datetime
from typing import List, Tuple
import logging


class T1DControllerWalsh(Controller):
    """
    Advanced hybrid closed-loop controller using:
      - Walsh IOB model (no curve fitting)
      - 3-minute control interval
      - Dynamic PID with aggression scaling
      - Meal + correction boluses
      - Super Micro Boluses (SMB)
      - Hypoglycemia safety
    
    CRITICAL FIX: SimGlucose pump directly injects basal value per timestep!
    - Controller returns basal in UNITS (not U/hr)
    - This is the actual amount injected over the 3-minute period
    - Conversion: basal_per_step = (desired_U_per_hr / 60) * dt_min
    """

    def __init__(self, profile: dict, logger: PatientLogger = None):
        self.p = profile
        self.logger = logger

        # State
        self.integral = 0.0
        self.glucose_hist: List[float] = []
        self.insulin_history: List[List[float]] = []  # [age_min, insulin_U]
        self.iob = 0.0
        self.sample_time = 3.0  # Fixed 3-minute steps

    # ====================== WALSH IOB MODEL ======================
    def _iob_walsh(self) -> float:
        """Calculate insulin on board using Walsh exponential decay model."""
        if not self.insulin_history:
            return 0.0

        iob = 0.0
        for entry in self.insulin_history:
            age_min = max(entry[0], 0.0)
            if age_min <= 180:
                # First 3 hours: quadratic decay
                frac = 1.0 - 0.5 * (age_min / 180) ** 2
            else:
                # After 3 hours: exponential tail
                frac = 0.5 * np.exp(-(age_min - 180) / 120)
            iob += entry[1] * frac
        return max(iob, 0.0)

    def _update_iob(self, basal_delivered_U: float, bolus_u: float, dt_min: float):
        """
        Update insulin history and recalculate IOB.
        
        Args:
            basal_delivered_U: Actual basal insulin delivered in this step (U)
            bolus_u: Bolus amount in U
            dt_min: Time step in minutes
        """
        total_delivered = basal_delivered_U + bolus_u
        
        if total_delivered > 1e-6:
            self.insulin_history.append([0.0, total_delivered])

        # Age all insulin packets
        for entry in self.insulin_history:
            entry[0] += dt_min

        # Recalculate IOB using Walsh model
        self.iob = self._iob_walsh()

        # Remove insulin older than DIA + buffer
        max_age = self.p.get("DIA", 300) + 180
        self.insulin_history = [e for e in self.insulin_history if e[0] < max_age]

    # ====================== TREND ESTIMATION ======================
    def _trend_mgdl_per_min(self) -> float:
        """Estimate glucose trend using linear regression on recent history."""
        if len(self.glucose_hist) < 4:
            return 0.0
        
        recent = np.array(self.glucose_hist[-10:])
        if len(recent) < 2:
            return 0.0
        
        x = np.arange(len(recent)) * self.sample_time
        slope = np.polyfit(x, recent, 1)[0]
        return slope

    # ====================== DYNAMIC AGGRESSION ======================
    def _compute_aggression(self, cgm: float, trend: float) -> float:
        """
        Compute dynamic aggression factor (0 to 1) based on glucose state.
        Higher aggression = more insulin delivery allowed.
        """
        aggression = 1.0

        # 1. IOB suppression (safety: reduce aggression when IOB is high)
        excess_iob = max(0.0, self.iob - self.p.get("iob_excess_threshold", 1.5))
        aggression *= max(0.55, 1.0 - 0.45 * excess_iob)

        # 2. Suppress when low and falling fast
        if cgm < 120 and trend < -2.0:
            supp = min(0.3, max(0.0, (-trend - 2.0) * 0.15))
            aggression *= (1.0 - supp)

        # 3. Boost when very high
        if cgm > 200:
            boost = min(0.25, (cgm - 200) / 400)
            aggression = min(1.0, aggression + boost)

        # 4. Boost when high and not falling
        if cgm > 180 and trend > -0.5:
            boost = min(0.3, (cgm - 180) / 300)
            aggression = min(1.0, aggression + boost)

        # Final clamp
        min_aggr = self.p.get("aggression_min", 0.2)
        return float(np.clip(aggression, min_aggr, 1.0))

    # ====================== PID BASAL ======================
    def _compute_pid_basal(self, error: float, trend: float, aggression: float) -> float:
        """
        Compute basal insulin amount for this timestep using PID controller.
        
        Returns: Basal insulin in UNITS to deliver over the 3-minute period
        """
        # Update integral with anti-windup
        dt_hours = self.sample_time / 60.0
        self.integral += error * dt_hours
        
        # Anti-windup: limit integral based on typical error magnitude
        max_integral = 500.0  # Allows for sustained ~50 mg/dL error
        self.integral = np.clip(self.integral, -max_integral, max_integral)

        # PID terms (all in mg/dL space)
        p_term = self.p["Kp"] * error
        i_term = self.p["Ki"] * self.integral
        d_term = self.p["Kd"] * (trend * 60)  # Convert mg/dL/min to mg/dL/hr

        # Total deviation in U/hr
        deviation_u_hr = (p_term + i_term + d_term) * aggression

        # Target basal rate in U/hr
        basal_u_hr = self.p["basal_nominal"] + deviation_u_hr
        basal_u_hr = float(np.clip(basal_u_hr, 0.0, self.p.get("basal_max", 2.5)))

        # CRITICAL: Convert U/hr to U per 3-minute timestep
        basal_per_step = (basal_u_hr / 60.0) * self.sample_time

        return basal_per_step

    # ====================== MEAL + CORRECTION BOLUS ======================
    def _compute_meal_bolus(self, cho: float, cgm: float, aggression: float) -> float:
        """
        Compute meal and correction bolus.
        
        Returns: Total bolus in U
        """
        if cho < 1.0:
            return 0.0

        # Carb bolus
        CR = self.p["CR"]  # grams per unit
        carb_bolus = cho / CR

        # Correction bolus with dynamic CF
        # FIXED: Lower CF = more aggressive (divide by aggression)
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        
        target = self.p["target"]
        correction_raw = max(0.0, (cgm - target) / CF_adjusted)

        # IOB-aware correction: subtract portion of IOB
        iob_offset = self.iob * 0.5  # Conservative: assume 50% effect
        correction = max(0.0, correction_raw - iob_offset)

        total_bolus = carb_bolus + correction
        
        # Safety cap
        max_bolus = self.p.get("max_bolus", 15.0)
        return min(total_bolus, max_bolus)

    # ====================== SUPER MICRO BOLUS (SMB) ======================
    def _compute_smb(self, cgm: float, trend: float, aggression: float):
        """
        Compute Super Micro Bolus for predictive insulin delivery.
        
        Returns: (smb_bolus_U, basal_reduction_U)
        """
        # Only trigger SMB if clearly rising with low IOB
        if not (cgm > 150 and trend > 0.5 and self.iob < 2.0):
            return 0.0, 0.0

        # 30-minute projection
        eventual_bg = cgm + trend * 30
        if eventual_bg <= 170:
            return 0.0, 0.0

        # Calculate needed insulin
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        needed = max(0.0, eventual_bg - self.p["target"]) / CF_adjusted
        
        # Conservative SMB: only give a fraction
        smb = min(0.3, needed * 0.25)

        if smb < 0.03:
            return 0.0, 0.0

        # Reduce basal to compensate (in U for this 3-min step)
        # smb is in U, so reduce basal proportionally
        basal_reduction = smb * 0.6  # Reduce by 60% of SMB amount
        
        return smb, basal_reduction

    # ====================== HYPO SAFETY ======================
    def _apply_hypo_safety(self, cgm: float, trend: float, iob: float, basal: float) -> float:
        """
        Apply hypoglycemia safety rules to basal amount.
        
        Args:
            basal: Proposed basal amount in U (for this 3-min step)
            
        Returns: Safe basal amount in U (for this 3-min step)
        """
        # Emergency stop
        if cgm < 65 or iob > 4.0:
            return 0.0

        # Strong fall + low glucose + high IOB
        if trend < -2.5 and cgm < 120 and iob > 2.0:
            return 0.0

        # Low glucose + high IOB: significant reduction
        if cgm < 90 and iob > 1.5:
            return max(0.3 * basal, 0.003)  # Keep minimum ~0.06 U/hr equivalent

        # Moderate fall approaching low range
        if trend < -1.8 and cgm < 130:
            return 0.7 * basal

        return basal

    # ====================== RESET ======================
    def reset(self):
        """Reset controller state for new episode."""
        self.integral = 0.0
        self.glucose_hist.clear()
        self.insulin_history.clear()
        self.iob = 0.0

    # ====================== MAIN POLICY ======================
    def policy(self, observation, reward=None, done=None, **info):
        """
        Main control policy - called every 3 minutes.
        
        Returns: Action(basal=U_per_step, bolus=U)
        """
        # Force 3-minute timestep
        self.sample_time = 3.0
        dt_min = 3.0

        # Reset on new episode
        if info.get("new_episode", False):
            self.reset()
            if self.logger:
                self.logger.__init__(self.logger.patient_name)
            logging.info("New episode -> Controller reset.")

        # Extract current state
        cgm = float(observation.CGM)
        cho = info['meal']
        time = info.get("time", datetime.now())
        step = info.get("step", 0)

        # Update glucose history
        self.glucose_hist.append(cgm)
        if len(self.glucose_hist) > 200:
            self.glucose_hist.pop(0)

        trend = self._trend_mgdl_per_min()

        # Emergency hypoglycemia shutdown
        if cgm < 65:
            action = Action(basal=0.0, bolus=0.0)
            aggression = 0.0
        else:
            # Normal control logic
            error = cgm - self.p["target"]
            aggression = self._compute_aggression(cgm, trend)

            # Compute basal (U per 3-min step)
            basal = self._compute_pid_basal(error, trend, aggression)
            
            # Compute meal/correction bolus (U)
            bolus = self._compute_meal_bolus(cho, cgm, aggression)

            # Add SMB if conditions are right
            smb, basal_reduction = self._compute_smb(cgm, trend, aggression)
            bolus += smb
            basal = max(0.0, basal - basal_reduction)

            # Apply hypoglycemia safety (last check)
            basal = self._apply_hypo_safety(cgm, trend, self.iob, basal)

            action = Action(basal=basal, bolus=bolus)

        # Update IOB tracking
        # basal is already in U delivered over this step
        self._update_iob(action.basal, action.bolus, dt_min)

        # Logging - convert basal to U/hr for readability
        basal_u_hr = (action.basal / dt_min) * 60.0
        
        if self.logger:
            self.logger.log_step(
                step=step,
                time=time,
                cgm=cgm,
                basal=basal_u_hr,  # Log as U/hr for human readability
                bolus=action.bolus,
                iob=self.iob,
                iob_model="WALSH",
                cho=cho,
                aggression=aggression,
                trend=trend,
                target=self.p["target"],
            )

        return action