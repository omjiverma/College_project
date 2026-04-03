import numpy as np
from simglucose.controller.base import Controller, Action
from src.models.iob import WalshIOB
from datetime import datetime
import logging
from collections import deque

class PID(Controller):
    def __init__(self, logger=None, dia=300.0, patient_type="SENSITIVE_ADOLESCENT"):
        if patient_type not in ["NORMAL_ADOLESCENT", "SENSITIVE_ADOLESCENT", "RESISTANT_ADOLESCENT", "NORMAL_ADULT", "SENSITIVE_ADULT"]:
            raise ValueError("Invalid patient_type")
        
        self.logger = logger
        self.sample_time = 3.0 # min
        self.last_cgm = None
        self.patient_type = patient_type

        self.iob_model = WalshIOB(dia_minutes=dia)
        self.cgm_history = deque(maxlen=20)
        self.cho_history = deque(maxlen=20)
        self.time_in_high = 0
        self.high_threshold = 180.0
        self.time_high_force_threshold = 30 
        
        # SMB Cooldown Tracker
        self.minutes_since_last_smb = 999.0 
        self.smb_cooldown_time = 45.0 

        # Kalman Filter initialization
        self.x = np.array([140.0, 0.0])
        self.P = np.eye(2) * 100.0
        self.Q = np.array([[0.1, 0.0], [0.0, 0.5]])
        self.R = 25.0
        self.H = np.array([[1.0, 0.0]])
        self.F = np.array([[1.0, self.sample_time / 60.0], [0.0, 1.0]])

        # PID State Variables
        self.integral_error = 0.0
        self.basal_adapt_factor = 1.0
        self.cr_adapt_factor = 1.0

    def reset(self):
        self.last_cgm = None
        self.cgm_history.clear()
        self.cho_history.clear()
        self.time_in_high = 0
        self.x = np.array([140.0, 0.0])
        self.P = np.eye(2) * 100.0
        self.iob_model.reset()
        self.integral_error = 0.0
        self.minutes_since_last_smb = 999.0

    def _kalman_update(self, z):
        x_pred = self.F @ self.x
        P_pred = self.F @ self.P @ self.F.T + self.Q
        y = z - self.H @ x_pred
        S = self.H @ P_pred @ self.H.T + self.R
        K = P_pred @ self.H.T / S
        self.x = x_pred + (K @ y).flatten()
        self.P = (np.eye(2) - K @ self.H) @ P_pred
        return self.x[0], self.x[1]

    def _calculate_poly_trend_and_prediction(self, horizon_mins=30.0):
        if len(self.cgm_history) < 3:
            return 0.0, (float(self.cgm_history[-1]) if self.cgm_history else 140.0)

        pts = list(self.cgm_history)[-6:]
        y = np.array(pts)
        t = np.arange(len(y)) * self.sample_time

        poly = np.polyfit(t, y, 1)
        rate_of_change = poly[0]

        t_current = t[-1]
        t_future = t_current + horizon_mins
        predicted_cgm = poly[0] * t_future + poly[1]

        return rate_of_change, predicted_cgm

    def policy(self, observation, reward=None, done=None, **info):
        if info.get("new_episode", False):
            self.reset()
        
        self.minutes_since_last_smb += self.sample_time
        
        raw_cgm = float(observation.CGM)
        now = info.get("time", datetime.now())
        meal_cho = info.get("meal", 0.0)
        basal_nominal = 1.0
        
        # ------------------------------------------------------------------
        # PID GAINS & PROFILE PARAMETERS
        # ------------------------------------------------------------------
        if self.patient_type == "NORMAL_ADOLESCENT":
            cr_nominal, smb_threshold, smb_scaler = 16.0, 190.0, 0.20
            Kp, Ki, Kd, target_cgm = 0.0012, 0.00005, 0.12, 145.0
        elif self.patient_type == "SENSITIVE_ADOLESCENT":
            cr_nominal, smb_threshold, smb_scaler = 15.0, 170.0, 0.15
            Kp, Ki, Kd, target_cgm = 0.0008, 0.00003, 0.18, 150.0
        elif self.patient_type == "RESISTANT_ADOLESCENT":
            cr_nominal, smb_threshold, smb_scaler = 4.0, 150.0, 0.5
            Kp, Ki, Kd, target_cgm = 0.025, 0.0010, 0.15, 120.0
        elif self.patient_type == "NORMAL_ADULT":
            cr_nominal, smb_threshold, smb_scaler = 7.0, 170.0, 0.5
            Kp, Ki, Kd, target_cgm = 0.015, 0.0004, 0.08, 120.0
        elif self.patient_type == "SENSITIVE_ADULT":
            cr_nominal, smb_threshold, smb_scaler = 9.0, 150.0, 0.4 
            Kp, Ki, Kd, target_cgm = 0.008, 0.0001, 0.04, 135.0

        self.cgm_history.append(raw_cgm)
        self.cho_history.append(meal_cho)

        filtered_cgm, _ = self._kalman_update(raw_cgm)
        poly_rate, predicted_cgm = self._calculate_poly_trend_and_prediction(horizon_mins=30.0)
        current_iob = np.clip(self.iob_model.calculate(), 0, 15)
        self.last_cgm = raw_cgm

        if filtered_cgm > self.high_threshold:
            self.time_in_high += self.sample_time
        else:
            self.time_in_high = max(0, self.time_in_high - self.sample_time * 2)

        # ==================================================================
        # PID MATH COMPUTATION (With Anti-Windup Flush)
        # ==================================================================
        error = filtered_cgm - target_cgm
        
        if error > 0 and poly_rate > -0.1:
            self.integral_error += error * self.sample_time
        else:
            self.integral_error = 0.0 # Instant flush to prevent multi-day drift
            
        self.integral_error = np.clip(self.integral_error, 0, 2000)
        
        pid_mult = 1.0 + (Kp * error) + (Ki * self.integral_error) + (Kd * poly_rate)
        pid_mult = np.clip(pid_mult, 0.0, 4.5) 

        # ------------------------------------------------------------------
        # ISOLATED SAFETY LAYERS (Insulin Feedback & PLGS)
        # ------------------------------------------------------------------
        if self.patient_type == "NORMAL_ADOLESCENT":
            safe_max_iob = (basal_nominal * 1.5) + 0.2
            final_basal_mult = pid_mult if filtered_cgm >= 120.0 else 0.0

            # tightened hypo protection for normal adolescents
            if filtered_cgm < 115.0 or predicted_cgm < 130.0:
                final_basal_mult = 0.0
            elif filtered_cgm < 160.0 and poly_rate < -0.35:
                final_basal_mult = 0.0
            elif sum(self.cho_history) > 20 and poly_rate < -0.3:
                final_basal_mult = 0.0 

            if poly_rate < -0.3 and current_iob > (safe_max_iob * 0.10):
                final_basal_mult = 0.0

            # proactive soft lock when IOB is high-facing downward trend
            if current_iob > (safe_max_iob * 0.35) and poly_rate < -0.2 and filtered_cgm < 170.0:
                final_basal_mult = 0.0

            # allow more insulin when stable high
            if filtered_cgm > 160.0 and poly_rate > -0.2 and current_iob < safe_max_iob * 0.8:
                final_basal_mult = max(final_basal_mult, 1.1)

            if self.time_in_high > self.time_high_force_threshold and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 200.0:
                    final_basal_mult = max(final_basal_mult, 1.2)

        elif self.patient_type == "SENSITIVE_ADOLESCENT":
            safe_max_iob = (basal_nominal * 1.7) + 0.05
            final_basal_mult = pid_mult if filtered_cgm >= 125 else 0.0
            if filtered_cgm < 120.0:
                final_basal_mult = 0.0
            if current_iob > safe_max_iob and filtered_cgm < 220.0:
                final_basal_mult = 0.0
            if predicted_cgm < 150.0 or (filtered_cgm < 145.0 and poly_rate < -0.4):
                final_basal_mult = 0.0
            if (sum(self.cho_history) > 15 and poly_rate < -0.25) or (poly_rate < -0.6 and current_iob > (safe_max_iob * 0.10)):
                final_basal_mult = 0.0
            if current_iob > (safe_max_iob * 0.25) and poly_rate < -0.3:
                final_basal_mult = 0.0
            if self.time_in_high > self.time_high_force_threshold and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 220.0:
                    final_basal_mult = max(final_basal_mult, 1.2)

        elif self.patient_type == "RESISTANT_ADOLESCENT":
            safe_max_iob = (basal_nominal * 2.0) + 0.1 
            final_basal_mult = pid_mult if filtered_cgm >= 100 else 0.0
            if predicted_cgm < 140.0 and poly_rate < -0.1: final_basal_mult = 0.0
            elif filtered_cgm < 120.0 and poly_rate < 0.0: final_basal_mult = 0.0
            elif sum(self.cho_history) > 15 and poly_rate < -0.3: final_basal_mult = 0.0 
            if poly_rate < 0 and current_iob > (safe_max_iob * 0.05): final_basal_mult = 0.0
            if filtered_cgm > 180.0 and poly_rate > 0.0: final_basal_mult = max(final_basal_mult, 2.0) 
            if self.time_in_high > 20 and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 200.0:
                    if poly_rate > -0.1: final_basal_mult = max(final_basal_mult, 3.0) 

        elif self.patient_type == "NORMAL_ADULT":
            safe_max_iob = (basal_nominal * 3.0) + 0.3
            final_basal_mult = pid_mult if filtered_cgm >= 100 else 0.0
            if predicted_cgm < 110.0 or (filtered_cgm < 115.0 and poly_rate < -1.0) or (sum(self.cho_history) > 20 and poly_rate < -1.0):
                final_basal_mult = 0.0
            if poly_rate < -1.0 and current_iob > (safe_max_iob * 0.25): final_basal_mult = 0.0
            if self.time_in_high > self.time_high_force_threshold and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 200.0:
                    final_basal_mult = max(final_basal_mult, 1.8)

        elif self.patient_type == "SENSITIVE_ADULT":
            safe_max_iob = (basal_nominal * 2.0) + 0.1 
            final_basal_mult = pid_mult if filtered_cgm >= 120 else 0.0
            if predicted_cgm < 130.0 or (filtered_cgm < 140.0 and poly_rate < -0.5) or (sum(self.cho_history) > 15 and poly_rate < -0.5):
                final_basal_mult = 0.0
            if poly_rate < -0.2 and current_iob > (safe_max_iob * 0.10): final_basal_mult = 0.0

        if current_iob > safe_max_iob and poly_rate < 0.5:
            final_basal_mult = 0.0

        final_basal_mult = final_basal_mult * self.basal_adapt_factor
        basal_step = (basal_nominal * final_basal_mult / 60.0) * self.sample_time
        bolus = (meal_cho / cr_nominal) if meal_cho > 0 else 0.0

        # ==================================================================
        # SUPER MICRO-BOLUS (SMB) LAYER WITH COOLDOWN
        # ==================================================================
        if bolus == 0 and filtered_cgm > smb_threshold and poly_rate > 0.0:
            if self.minutes_since_last_smb >= self.smb_cooldown_time and current_iob < (safe_max_iob * 0.40):
                smb_dose = basal_nominal * smb_scaler 
                bolus += smb_dose
                final_basal_mult = 0.0
                self.minutes_since_last_smb = 0.0 
                iob_model_log = f"SMB_{self.patient_type.upper()}"
            elif bolus > 0 and filtered_cgm > smb_threshold + 30 and poly_rate > 0.5 and self.minutes_since_last_smb >= self.smb_cooldown_time:
                smb_dose = basal_nominal * smb_scaler * 2
                bolus += smb_dose
                final_basal_mult = 0.0
                self.minutes_since_last_smb = 0.0 
                iob_model_log = f"SMB_{self.patient_type.upper()}"
            else:
                iob_model_log = f"PID_{self.patient_type.upper()}"
        else:
            iob_model_log = f"PID_{self.patient_type.upper()}"

        # emergency hypo lockout
        if filtered_cgm < 70.0 or (filtered_cgm < 80.0 and poly_rate < -1.0):
            basal_step = 0.0
            bolus = 0.0
            self.integral_error = 0.0
            iob_model_log = f"HYPO_SAFE_{self.patient_type.upper()}"

        self.iob_model.update(basal_step, bolus, self.sample_time)

        if self.logger:
            self.logger.log_step(
                step=info.get("step", 0),
                time=now,
                cgm=raw_cgm,
                basal=basal_step,
                bolus=bolus,
                iob=current_iob,
                iob_model=iob_model_log, 
                cho=meal_cho,
                aggression=final_basal_mult,
                trend=poly_rate,
                target=target_cgm,
                filtered_cgm=filtered_cgm,
                predicted_cgm=predicted_cgm,
            )

        return Action(basal=basal_step, bolus=bolus)