# src/controller/walsh_hpc.py
"""T1D Hybrid Controller with Walsh IOB and Bergman HPC."""

from simglucose.controller.base import Controller, Action
from src.models.bergman import BergmanMinimalModel
from src.models.iob import WalshIOB
from src.utils.logging import PatientLogger

import numpy as np
from datetime import datetime, timedelta
from typing import List, Tuple
import logging


class T1DControllerWalsh(Controller):
    """
    Hybrid T1D controller combining:
      - Walsh IOB model
      - PID baseline with dynamic aggression
      - SMB (Super Micro Bolus)
      - Bergman Minimal Model based Predictive Control (HPC)
      - Hypoglycemia safety
      - Diagnostic logging for tuning
    """

    def __init__(self, profile: dict, logger: PatientLogger = None):
        """
        Initialize controller with profile and optional logger.
        
        Args:
            profile: Dictionary with controller parameters
            logger: Optional PatientLogger for diagnostic data
        """
        self.p = profile
        self.logger = logger

        # State tracking
        self.integral = 0.0
        self.glucose_hist: List[float] = []
        self.iob = 0.0

        # 3-minute timestep (consistent with simglucose)
        self.sample_time = 3.0

        # Initialize models
        self.iob_model = WalshIOB(dia_minutes=self.p.get("DIA", 300.0))
        self.bergman = BergmanMinimalModel(profile)

        # MPC/HPC configuration (user-configurable via YAML)
        self.mpc_enable = bool(self.p.get("mpc_enable", True))
        self.mpc_horizon = float(self.p.get("mpc_horizon", 30.0))            # minutes
        self.mpc_immediate_fraction = float(self.p.get("mpc_immediate_fraction", 0.25))
        self.mpc_max_bolus = float(self.p.get("mpc_max_bolus", 1.0))        # U cap
        self.mpc_safety_fraction = float(self.p.get("mpc_safety_fraction", 0.25))

        # Safety tracking
        self.hypo_events: List[float] = []
        self.mpc_disabled_until = None

    # ---- Trend Estimation ----
    def _trend_mgdl_per_min(self) -> float:
        """Estimate raw glucose trend from recent history."""
        if len(self.glucose_hist) < 4:
            return 0.0
        recent = np.array(self.glucose_hist[-10:])
        x = np.arange(len(recent)) * self.sample_time
        slope = np.polyfit(x, recent, 1)[0]
        return float(slope)

    def _filtered_trend(self) -> float:
        """Estimate filtered glucose trend using weighted average."""
        if len(self.glucose_hist) < 6:
            return self._trend_mgdl_per_min()
        window = np.array(self.glucose_hist[-6:])
        weights = np.array([1.0, 2.0, 3.0, 3.0, 2.0, 1.0])
        sm = np.dot(window, weights) / weights.sum()
        raw = self.glucose_hist[-1]
        return float((raw - sm) / self.sample_time)

    # ---- Dynamic Aggression ----
    def _compute_aggression(self, cgm: float, trend: float) -> float:
        """
        Compute dynamic aggression factor (0.2-1.0).
        Reduces aggression with excess IOB, increases when running high.
        """
        aggression = 1.0
        
        # Suppress aggression if IOB is high
        excess_iob = max(0.0, self.iob - self.p.get("iob_excess_threshold", 1.5))
        aggression *= max(0.55, 1.0 - 0.45 * excess_iob)
        
        # Suppress aggression if trending down and low
        if cgm < 120 and trend < -2.0:
            supp = min(0.3, max(0.0, (-trend - 2.0) * 0.15))
            aggression *= (1.0 - supp)
        
        # Boost aggression if running high
        if cgm > 200:
            boost = min(0.25, (cgm - 200) / 400.0)
            aggression = min(1.0, aggression + boost)
        
        # Boost if elevated and not trending down
        if cgm > 180 and trend > -0.5:
            boost = min(0.3, (cgm - 180) / 300.0)
            aggression = min(1.0, aggression + boost)
        
        return float(np.clip(aggression, self.p.get("aggression_min", 0.2), 1.0))

    # ---- PID Basal Insulin ----
    def _compute_pid_basal(self, error: float, trend: float, aggression: float) -> float:
        """
        Compute PID-based basal insulin for this 3-min step.
        
        Args:
            error: Glucose error (actual - target)
            trend: Glucose trend (mg/dL/min)
            aggression: Aggression factor (0-1)
            
        Returns:
            Basal insulin for 3-min step in U
        """
        dt_hours = self.sample_time / 60.0
        self.integral += error * dt_hours
        max_integral = 500.0
        self.integral = np.clip(self.integral, -max_integral, max_integral)
        
        # PID terms
        p_term = self.p["Kp"] * error
        i_term = self.p["Ki"] * self.integral
        d_term = self.p["Kd"] * (trend * 60.0)
        
        # Deviation basal (U/hr)
        deviation_u_hr = (p_term + i_term + d_term) * aggression
        basal_u_hr = self.p["basal_nominal"] + deviation_u_hr
        basal_u_hr = float(np.clip(basal_u_hr, 0.0, self.p.get("basal_max", 2.5)))
        
        # Convert to 3-min step
        basal_per_step = (basal_u_hr / 60.0) * self.sample_time
        return float(basal_per_step)

    # ---- Meal & Correction Bolus ----
    def _compute_meal_bolus(self, cho: float, cgm: float, aggression: float) -> float:
        """
        Compute meal and correction bolus.
        
        Args:
            cho: Carbs informed (g)
            cgm: Current glucose (mg/dL)
            aggression: Aggression factor (0-1)
            
        Returns:
            Bolus insulin in U
        """
        if cho < 1.0:
            return 0.0
        
        # Carb bolus
        CR = self.p["CR"]
        carb_bolus = cho / CR
        
        # Correction bolus (with aggression adjustment)
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        correction_raw = max(0.0, (cgm - self.p["target"]) / CF_adjusted)
        
        # IOB offset reduces correction
        iob_offset = self.iob * 0.5
        correction = max(0.0, correction_raw - iob_offset)
        
        total = carb_bolus + correction
        return float(min(total, self.p.get("max_bolus", 15.0)))

    # ---- Super Micro Bolus (SMB) ----
    def _compute_smb(self, cgm: float, trend: float, aggression: float) -> Tuple[float, float]:
        """
        Compute Super Micro Bolus and basal reduction.
        
        Returns:
            Tuple of (smb_bolus_U, basal_reduction_U)
        """
        # Only SMB if conditions are favorable
        if not (cgm > 150 and trend > 0.5 and self.iob < 2.0):
            return 0.0, 0.0
        
        # Project to 30 minutes
        eventual = cgm + trend * 30.0
        if eventual <= 170:
            return 0.0, 0.0
        
        # Compute needed correction
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        needed = max(0.0, eventual - self.p["target"]) / CF_adjusted
        smb = min(0.3, needed * 0.25)
        
        if smb < 0.03:
            return 0.0, 0.0
        
        return float(smb), float(smb * 0.6)

    # ---- Hypoglycemia Safety ----
    def _apply_hypo_safety(self, cgm: float, trend: float, iob: float, basal: float) -> float:
        """
        Apply hypoglycemia safety checks to limit basal.
        
        Returns:
            Adjusted basal insulin
        """
        # Critical low - suspend basal
        if cgm < 65 or iob > 4.0:
            return 0.0
        
        # Steep decline with moderate IOB - suspend
        if trend < -2.5 and cgm < 120 and iob > 2.0:
            return 0.0
        
        # Low glucose - reduce basal
        if cgm < 90 and iob > 1.5:
            return max(0.3 * basal, 0.003)
        
        # Declining from moderate - reduce basal
        if trend < -1.8 and cgm < 130:
            return 0.7 * basal
        
        return basal

    # ---- Bergman Predictive Control (HPC) ----
    def _bergman_hpc(self, cgm: float, trend: float, basal_current: float) -> Tuple[float, float]:
        """
        Heuristic predictive controller using Bergman model.
        Projects glucose forward and adjusts insulin delivery.
        
        Returns:
            Tuple of (adjusted_basal_U, predicted_bg_at_horizon)
        """
        horizon_min = float(self.p.get("mpc_horizon", self.mpc_horizon))
        dt = self.sample_time
        steps = max(1, int(round(horizon_min / dt)))

        # Predict without extra insulin to see natural trajectory
        G_sim, X_sim, I_sim = cgm, self.bergman.X_state, self.bergman.I_state
        
        for _ in range(steps):
            G_sim, X_sim, I_sim = self.bergman.step(G_sim, X_sim, I_sim, 0.0, 0.0, dt)

        predicted_bg = float(G_sim)
        delta = predicted_bg - float(self.p["target"])

        # No overshoot predicted - no action needed
        if delta <= 0:
            return basal_current, predicted_bg

        # Map BG delta to insulin needed using CF sensitivity
        CF = float(self.p.get("CF_base", self.p.get("CF", 50.0)))
        predicted_bolus = delta / max(CF, 1e-6)
        predicted_bolus = float(np.clip(predicted_bolus, 0.0, float(self.mpc_max_bolus)))

        # Deliver fraction immediately as extra basal
        frac = float(np.clip(self.mpc_immediate_fraction, 0.0, 0.4))
        immediate_U = predicted_bolus * frac
        new_basal = float(max(0.0, basal_current + immediate_U))

        # Update Bergman states optimistically (assume this insulin works)
        try:
            G1, X1, I1 = self.bergman.step(cgm, self.bergman.X_state, self.bergman.I_state, immediate_U, 0.0, dt)
            self.bergman.update_states(float(X1), float(I1))
        except Exception:
            pass

        return new_basal, predicted_bg

    # ---- Reset ----
    def reset(self):
        """Reset controller state for new episode."""
        self.integral = 0.0
        self.glucose_hist.clear()
        self.iob = 0.0
        self.hypo_events.clear()
        self.mpc_disabled_until = None
        self.iob_model.reset()
        self.bergman.reset()

    # ---- Main Policy ----
    def policy(self, observation, reward=None, done=None, **info):
        """
        Main control policy called at each simulation step.
        
        Args:
            observation: CGM observation
            reward: Reward signal (unused)
            done: Episode done flag
            **info: Meal info, time, step number
            
        Returns:
            Action with basal and bolus insulin
        """
        dt = self.sample_time

        # Reset on new episode
        if info.get("new_episode", False):
            self.reset()
            if self.logger:
                try:
                    self.logger.__init__(self.logger.patient_name)
                except Exception:
                    pass
            logging.info("Controller reset for new episode.")

        cgm = float(observation.CGM)
        cho = info.get("meal", 0.0)
        time = info.get("time", datetime.now())
        step = info.get("step", 0)

        # Track glucose history
        self.glucose_hist.append(cgm)
        if len(self.glucose_hist) > 500:
            self.glucose_hist.pop(0)

        # Compute trend and error
        trend = self._filtered_trend()
        error = cgm - self.p["target"]
        aggression = self._compute_aggression(cgm, trend)

        # Base PID basal
        basal = self._compute_pid_basal(error, trend, aggression)

        # MPC/HPC logging
        mpc_used = False
        mpc_predicted_bg = cgm
        mpc_bolus = 0.0

        # Decide if HPC should run (safety checks)
        effective_mpc = bool(self.p.get("mpc_enable", self.mpc_enable))
        if self.mpc_disabled_until is not None and time < self.mpc_disabled_until:
            effective_mpc = False

        # Run Bergman HPC if conditions met
        if effective_mpc and (cgm > self.p["target"]) and (trend > 0.0) and (self.iob < float(self.p.get("iob_mpc_threshold", 3.0))):
            try:
                new_basal, predicted_bg = self._bergman_hpc(cgm, trend, basal)
                mpc_bolus = max(0.0, (new_basal - basal) / max(self.mpc_immediate_fraction, 1e-6))
                mpc_bolus = float(min(mpc_bolus, self.mpc_max_bolus))
                basal = float(new_basal)
                mpc_used = True
                mpc_predicted_bg = float(predicted_bg)
            except Exception as e:
                logging.exception("Bergman HPC error: %s", e)
                mpc_used = False
                mpc_predicted_bg = cgm
                mpc_bolus = 0.0

        # Meal/correction bolus
        bolus = self._compute_meal_bolus(cho, cgm, aggression)

        # Super Micro Bolus
        smb, br = self._compute_smb(cgm, trend, aggression)
        bolus += smb
        basal = max(0.0, basal - br)

        # Hypoglycemia safety
        basal = self._apply_hypo_safety(cgm, trend, self.iob, basal)

        # Update IOB model
        self.iob_model.update(basal, bolus, dt)
        self.iob = self.iob_model.calculate()

        # Logging
        if self.logger:
            try:
                basal_u_hr = (basal / dt) * 60.0
                self.logger.log_step(
                    step=step,
                    time=time,
                    cgm=cgm,
                    basal=basal_u_hr,
                    bolus=bolus,
                    iob=self.iob,
                    iob_model="WALSH+BERGMAN_HPC",
                    cho=cho,
                    aggression=aggression,
                    trend=trend,
                    target=self.p["target"],
                    mpc_used=int(mpc_used),
                    mpc_bolus=round(float(mpc_bolus), 4),
                    mpc_predicted_bg=round(float(mpc_predicted_bg), 2)
                )
            except Exception:
                logging.exception("Controller logging failed.")

        return Action(basal=basal, bolus=bolus)
