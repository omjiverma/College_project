# controller.py
# T1DControllerWalsh with Bergman Minimal Model predictive control (HPC)
# Pure Python implementation — NO CVXPY / NO external solver required.
# Preserves Walsh IOB, PID, SMB, hypo safety, and logging. Based on original. :contentReference[oaicite:2]{index=2}

from simglucose.controller.base import Controller, Action
from utils.logging import PatientLogger

import numpy as np
from datetime import datetime, timedelta
from typing import List
import logging


class T1DControllerWalsh(Controller):
    """
    Hybrid controller:
      - Walsh IOB model
      - PID baseline with dynamic aggression
      - SMB + meal/correction bolus
      - Bergman Minimal Model based Predictive Controller (heuristic, no solver)
      - Hypoglycemia safety
      - Logs MPC diagnostics for tuning
    """

    def __init__(self, profile: dict, logger: PatientLogger = None):
        self.p = profile
        self.logger = logger

        # State
        self.integral = 0.0
        self.glucose_hist: List[float] = []
        self.insulin_history: List[List[float]] = []  # [age_min, U]
        self.iob = 0.0

        # 3-minute timestep
        self.sample_time = 3.0

        # Bergman internal states (insulin action X, plasma insulin I)
        self.X_state = 0.0
        self.I_state = float(self.p.get("Ib", 15.0))

        # MPC/HPC defaults (user-configurable in YAML)
        self.mpc_enable = bool(self.p.get("mpc_enable", True))
        self.mpc_horizon = float(self.p.get("mpc_horizon", 30.0))            # minutes
        self.mpc_immediate_fraction = float(self.p.get("mpc_immediate_fraction", 0.25))
        self.mpc_max_bolus = float(self.p.get("mpc_max_bolus", 1.0))        # U cap on predicted bolus
        self.mpc_safety_fraction = float(self.p.get("mpc_safety_fraction", 0.25))  # fraction of predicted bolus to deliver now

        # Safety analytics
        self.hypo_events: List[float] = []
        self.mpc_disabled_until = None

    # ---------------- WALSH IOB ----------------
    def _iob_walsh(self) -> float:
        if not self.insulin_history:
            return 0.0
        iob = 0.0
        for age, u in self.insulin_history:
            age_min = max(age, 0.0)
            if age_min <= 180:
                frac = 1.0 - 0.5 * (age_min / 180.0) ** 2
            else:
                frac = 0.5 * np.exp(-(age_min - 180.0) / 120.0)
            iob += u * frac
        return max(iob, 0.0)

    def _update_iob(self, basal_delivered_U: float, bolus_u: float, dt_min: float):
        total = basal_delivered_U + bolus_u
        if total > 1e-8:
            self.insulin_history.append([0.0, total])
        # age
        for pkt in self.insulin_history:
            pkt[0] += dt_min
        max_age = self.p.get("DIA", 300) + 180
        self.insulin_history = [pkt for pkt in self.insulin_history if pkt[0] < max_age]
        self.iob = self._iob_walsh()

    # ---------------- Trend Estimation ----------------
    def _trend_mgdl_per_min(self) -> float:
        if len(self.glucose_hist) < 4:
            return 0.0
        recent = np.array(self.glucose_hist[-10:])
        x = np.arange(len(recent)) * self.sample_time
        slope = np.polyfit(x, recent, 1)[0]
        return slope

    def _filtered_trend(self) -> float:
        if len(self.glucose_hist) < 6:
            return self._trend_mgdl_per_min()
        window = np.array(self.glucose_hist[-6:])
        weights = np.array([1.0, 2.0, 3.0, 3.0, 2.0, 1.0])
        sm = np.dot(window, weights) / weights.sum()
        raw = self.glucose_hist[-1]
        return (raw - sm) / self.sample_time

    # ---------------- Dynamic Aggression (same idea) ----------------
    def _compute_aggression(self, cgm: float, trend: float) -> float:
        aggression = 1.0
        excess_iob = max(0.0, self.iob - self.p.get("iob_excess_threshold", 1.5))
        aggression *= max(0.55, 1.0 - 0.45 * excess_iob)
        if cgm < 120 and trend < -2.0:
            supp = min(0.3, max(0.0, (-trend - 2.0) * 0.15))
            aggression *= (1.0 - supp)
        if cgm > 200:
            boost = min(0.25, (cgm - 200) / 400.0)
            aggression = min(1.0, aggression + boost)
        if cgm > 180 and trend > -0.5:
            boost = min(0.3, (cgm - 180) / 300.0)
            aggression = min(1.0, aggression + boost)
        return float(np.clip(aggression, self.p.get("aggression_min", 0.2), 1.0))

    # ---------------- PID Basal ----------------
    def _compute_pid_basal(self, error: float, trend: float, aggression: float) -> float:
        dt_hours = self.sample_time / 60.0
        self.integral += error * dt_hours
        max_integral = 500.0
        self.integral = np.clip(self.integral, -max_integral, max_integral)
        p_term = self.p["Kp"] * error
        i_term = self.p["Ki"] * self.integral
        d_term = self.p["Kd"] * (trend * 60.0)
        deviation_u_hr = (p_term + i_term + d_term) * aggression
        basal_u_hr = self.p["basal_nominal"] + deviation_u_hr
        basal_u_hr = float(np.clip(basal_u_hr, 0.0, self.p.get("basal_max", 2.5)))
        basal_per_step = (basal_u_hr / 60.0) * self.sample_time
        return basal_per_step

    # ---------------- Meal & Correction Bolus ----------------
    def _compute_meal_bolus(self, cho: float, cgm: float, aggression: float) -> float:
        if cho < 1.0:
            return 0.0
        CR = self.p["CR"]
        carb_bolus = cho / CR
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        correction_raw = max(0.0, (cgm - self.p["target"]) / CF_adjusted)
        iob_offset = self.iob * 0.5
        correction = max(0.0, correction_raw - iob_offset)
        total = carb_bolus + correction
        return float(min(total, self.p.get("max_bolus", 15.0)))

    # ---------------- SMB ----------------
    def _compute_smb(self, cgm: float, trend: float, aggression: float):
        if not (cgm > 150 and trend > 0.5 and self.iob < 2.0):
            return 0.0, 0.0
        eventual = cgm + trend * 30.0
        if eventual <= 170:
            return 0.0, 0.0
        CF_adjusted = self.p["CF_base"] / max(aggression, 0.1)
        needed = max(0.0, eventual - self.p["target"]) / CF_adjusted
        smb = min(0.3, needed * 0.25)
        if smb < 0.03:
            return 0.0, 0.0
        return smb, smb * 0.6

    # ---------------- Hypo safety ----------------
    def _apply_hypo_safety(self, cgm: float, trend: float, iob: float, basal: float) -> float:
        if cgm < 65 or iob > 4.0:
            return 0.0
        if trend < -2.5 and cgm < 120 and iob > 2.0:
            return 0.0
        if cgm < 90 and iob > 1.5:
            return max(0.3 * basal, 0.003)
        if trend < -1.8 and cgm < 130:
            return 0.7 * basal
        return basal

    # ---------------- Bergman model step (pure Python) ----------------
    def _bergman_step(self, G, X, I, U, D, dt):
        """
        One discrete Bergman step (Euler) with dt in minutes.
        G: mg/dL, X: insulin action, I: insulin conc (μU/mL), U: insulin delivered this step (U)
        D: meal appearance (mg/dL/min) - default 0
        """
        p1 = self.p.get("p1", 0.028)
        p2 = self.p.get("p2", 0.025)
        p3 = self.p.get("p3", 5e-5)
        n  = self.p.get("n", 0.05)
        Gb = self.p.get("Gb", 110.0)
        Ib = self.p.get("Ib", 15.0)
        alpha = self.p.get("alpha", 300.0)

        dG = -(p1 + X) * G + p1 * Gb + D
        dX = -p2 * X + p3 * (I - Ib)
        dI = -n * (I - Ib) + alpha * U

        Gn = G + dt * dG
        Xn = X + dt * dX
        In = I + dt * dI

        return Gn, Xn, In

    # ---------------- Predictive Bergman-based control (HPC, no optimizer) ----------------
    def _bergman_hpc(self, cgm, trend, basal_current) -> (float, float):
        """
        Heuristic predictive controller using Bergman model (no solver).
        Returns (basal_adjusted_U_for_this_step, predicted_bg_at_horizon)
        """
        # Defaults
        horizon_min = float(self.p.get("mpc_horizon", self.mpc_horizon))
        dt = self.sample_time
        steps = max(1, int(round(horizon_min / dt)))

        # Initial states
        Gsim, Xsim, Isim = cgm, self.X_state, self.I_state

        # 1) Simulate future trajectory WITHOUT extra insulin to see overshoot
        for _ in range(steps):
            Gsim, Xsim, Isim = self._bergman_step(Gsim, Xsim, Isim, 0.0, 0.0, dt)

        predicted_bg = float(Gsim)
        delta = predicted_bg - float(self.p["target"])

        # If no predicted overshoot, no action
        if delta <= 0:
            return basal_current, predicted_bg

        # Map delta BG -> insulin needed (simple sensitivity using CF)
        # CF: grams-per-U equivalent in mg/dL per U. Using CF_base approximates sensitivity.
        CF = float(self.p.get("CF_base", self.p.get("CF", 50.0)))
        # conservative predicted bolus in U
        predicted_bolus = delta / max(CF, 1e-6)

        # Safety cap
        predicted_bolus = float(np.clip(predicted_bolus, 0.0, float(self.p.get("mpc_max_bolus", self.mpc_max_bolus))))

        # Convert fraction to immediate basal delivered THIS 3-min step
        frac = float(self.p.get("mpc_immediate_fraction", self.mpc_immediate_fraction))
        frac = float(np.clip(frac, 0.0, 0.4))
        immediate_U = predicted_bolus * frac

        # Add to current basal (basal_current is U for this 3-min step)
        new_basal = float(max(0.0, basal_current + immediate_U))

        # Update internal Bergman states assuming this injection occurs now (conservative)
        try:
            G1, X1, I1 = self._bergman_step(cgm, self.X_state, self.I_state, immediate_U, 0.0, dt)
            self.X_state, self.I_state = float(X1), float(I1)
        except Exception:
            # if any numerical trouble, keep previous states
            pass

        return new_basal, predicted_bg

    # ---------------- Reset ----------------
    def reset(self):
        self.integral = 0.0
        self.glucose_hist.clear()
        self.insulin_history.clear()
        self.iob = 0.0
        self.X_state = 0.0
        self.I_state = float(self.p.get("Ib", 15.0))
        self.hypo_events.clear()
        self.mpc_disabled_until = None

    # ---------------- Main policy ----------------
    def policy(self, observation, reward=None, done=None, **info):
        dt = self.sample_time

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

        # update history
        self.glucose_hist.append(cgm)
        if len(self.glucose_hist) > 500:
            self.glucose_hist.pop(0)

        trend = self._filtered_trend()
        error = cgm - self.p["target"]
        aggression = self._compute_aggression(cgm, trend)

        # PID basal in U for this 3-min step
        basal = self._compute_pid_basal(error, trend, aggression)

        # Prepare MPC/HPC logging fields
        mpc_used = False
        mpc_predicted_bg = cgm
        mpc_bolus = 0.0

        # Decide if HPC runs (safety checks)
        effective_mpc = bool(self.p.get("mpc_enable", self.mpc_enable))
        if self.mpc_disabled_until is not None and time < self.mpc_disabled_until:
            effective_mpc = False

        if effective_mpc and (cgm > self.p["target"]) and (trend > 0.0) and (self.iob < float(self.p.get("iob_mpc_threshold", 3.0))):
            # Run Bergman-based predictive controller (heuristic)
            try:
                new_basal, predicted_bg = self._bergman_hpc(cgm, trend, basal)
                # compute approximate mpc_bolus for logging (reverse map)
                mpc_bolus = max(0.0, (new_basal - basal) / max(self.p.get("mpc_immediate_fraction", self.mpc_immediate_fraction), 1e-6))
                mpc_bolus = float(min(mpc_bolus, float(self.p.get("mpc_max_bolus", self.mpc_max_bolus))))
                basal = float(new_basal)
                mpc_used = True
                mpc_predicted_bg = float(predicted_bg)
            except Exception as e:
                logging.exception("Bergman HPC error: %s", e)
                # fallback: keep PID basal
                mpc_used = False
                mpc_predicted_bg = cgm
                mpc_bolus = 0.0

        # Meal/correction bolus
        bolus = self._compute_meal_bolus(cho, cgm, aggression)

        # SMB
        smb, br = self._compute_smb(cgm, trend, aggression)
        bolus += smb
        basal = max(0.0, basal - br)

        # Hypo safety
        basal = self._apply_hypo_safety(cgm, trend, self.iob, basal)

        # Update IOB with delivered insulin this step
        self._update_iob(basal, bolus, dt)

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
