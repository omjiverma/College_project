import numpy as np
import skfuzzy as fuzzy
from skfuzzy import control as ctrl
from simglucose.controller.base import Controller, Action
from src.models.iob import WalshIOB
from datetime import datetime
import logging
from collections import deque


class T1D_Fuzzy_Walsh_Controller(Controller):
    def __init__(self, logger=None, dia=300.0, patient_type="normal"):
        if patient_type not in ["normal", "sensitive", "resistant"]:
            raise ValueError("patient_type must be 'normal', 'sensitive', or 'resistant'")

        self.logger = logger
        self.sample_time = 3.0
        self.last_cgm = None
        self.patient_type = patient_type

        self.iob_model = WalshIOB(dia_minutes=dia)
        self.cgm_history = deque(maxlen=20)
        self.cho_history = deque(maxlen=20)
        self.time_in_high = 0
        self.high_threshold = 180.0
        self.time_high_force_threshold = 30 

        # Kalman Filter
        self.x = np.array([140.0, 0.0])
        self.P = np.eye(2) * 100.0
        self.Q = np.array([[0.1, 0.0], [0.0, 0.5]])
        self.R = 25.0
        self.H = np.array([[1.0, 0.0]])
        self.F = np.array([[1.0, self.sample_time / 60.0], [0.0, 1.0]])

        self.flc = self._build_fuzzy_system()

        self.basal_adapt_factor = 1.0
        self.cr_adapt_factor = 1.0

    def _build_fuzzy_system(self):
        univ_glucose = np.arange(30, 401, 1)      
        univ_trend   = np.arange(-12, 12.1, 0.4)  
        univ_iob_pct = np.arange(0, 101, 1)    
        univ_mult    = np.arange(0, 6.51, 0.05)   

        g = ctrl.Antecedent(univ_glucose, 'glucose')
        t = ctrl.Antecedent(univ_trend, 'trend')
        i = ctrl.Antecedent(univ_iob_pct, 'iob_pct')
        m = ctrl.Consequent(univ_mult, 'multiplier')

        # COMMON VARIABLES
        t['rapid_fall'] = fuzzy.trapmf(univ_trend, [-12, -12, -5.0, -3.0])
        t['falling']    = fuzzy.trimf(univ_trend, [-4.0, -1.5, -0.5])
        t['stable']     = fuzzy.trimf(univ_trend, [-0.8, 0, 0.8])
        t['rising']     = fuzzy.trimf(univ_trend, [0.5, 2.0, 4.0])
        t['rapid_rise'] = fuzzy.trapmf(univ_trend, [3.0, 6.0, 12, 12])

        i['very_low'] = fuzzy.trapmf(univ_iob_pct, [0, 0, 15, 30])
        i['low']      = fuzzy.trimf(univ_iob_pct, [20, 40, 60])
        i['moderate'] = fuzzy.trimf(univ_iob_pct, [50, 70, 85])
        i['high']     = fuzzy.trapmf(univ_iob_pct, [80, 95, 100, 100])

        if self.patient_type == "normal":
            g['severe_low'] = fuzzy.trapmf(univ_glucose, [30, 30, 75, 90])
            g['low']        = fuzzy.trimf(univ_glucose, [90, 100, 115])
            g['target']     = fuzzy.trimf(univ_glucose, [110, 130, 165])
            g['high']       = fuzzy.trimf(univ_glucose, [150, 200, 260])
            g['very_high']  = fuzzy.trapmf(univ_glucose, [240, 300, 400, 400])

            m['zero']     = fuzzy.trimf(univ_mult, [0.00, 0.00, 0.10])
            m['very_low'] = fuzzy.trimf(univ_mult, [0.05, 0.20, 0.60])
            m['low']      = fuzzy.trimf(univ_mult, [0.20, 0.60, 1.00])
            m['normal']   = fuzzy.trimf(univ_mult, [0.60, 1.00, 1.80])
            m['elevated'] = fuzzy.trimf(univ_mult, [1.00, 1.60, 2.40])
            m['high']     = fuzzy.trimf(univ_mult, [1.80, 2.40, 3.20])

            rules = [
                ctrl.Rule(g['severe_low'] | g['low'], m['zero']),
                ctrl.Rule((t['rapid_fall'] | t['falling']) & g['target'], m['zero']),
                ctrl.Rule(i['high'], m['zero']),
                ctrl.Rule(i['moderate'] & ~g['very_high'], m['very_low']),
                ctrl.Rule(g['very_high'] & t['rapid_rise'], m['high']),
                ctrl.Rule(g['very_high'] & i['very_low'], m['elevated']),
                ctrl.Rule(g['high'] & t['rising'] & i['very_low'], m['elevated']),
                ctrl.Rule(g['target'] & t['stable'] & i['very_low'], m['normal']),
                ctrl.Rule(g['high'] & t['falling'], m['low']),
            ]

        elif self.patient_type == "sensitive":
            g['severe_low'] = fuzzy.trapmf(univ_glucose, [30, 30, 85, 105])
            g['low']        = fuzzy.trimf(univ_glucose, [95, 115, 135])
            g['target']     = fuzzy.trimf(univ_glucose, [125, 150, 190]) 
            g['high']       = fuzzy.trimf(univ_glucose, [170, 230, 290])
            g['very_high']  = fuzzy.trapmf(univ_glucose, [270, 330, 400, 400])

            m['zero']     = fuzzy.trimf(univ_mult, [0.00, 0.00, 0.10])
            m['very_low'] = fuzzy.trimf(univ_mult, [0.05, 0.10, 0.30])
            m['low']      = fuzzy.trimf(univ_mult, [0.10, 0.30, 0.60])
            m['normal']   = fuzzy.trimf(univ_mult, [0.40, 0.70, 1.00])
            m['elevated'] = fuzzy.trimf(univ_mult, [0.80, 1.10, 1.40])
            m['high']     = fuzzy.trimf(univ_mult, [1.20, 1.50, 1.80])

            rules = [
                ctrl.Rule(g['severe_low'] | g['low'], m['zero']),
                ctrl.Rule((t['rapid_fall'] | t['falling']) & (g['target'] | g['low']), m['zero']),
                ctrl.Rule(i['high'], m['zero']),
                ctrl.Rule(i['moderate'] & ~g['very_high'], m['very_low']),
                ctrl.Rule(g['very_high'] & t['rapid_rise'] & ~i['high'], m['high']), 
                ctrl.Rule(g['very_high'] & i['very_low'], m['elevated']),
                ctrl.Rule(g['target'] & t['stable'] & i['very_low'], m['normal']),
            ]

        elif self.patient_type == "resistant":
            g['severe_low'] = fuzzy.trapmf(univ_glucose, [30, 30, 75, 95])
            g['low']        = fuzzy.trimf(univ_glucose, [85, 105, 125])
            g['target']     = fuzzy.trimf(univ_glucose, [115, 135, 160]) 
            g['high']       = fuzzy.trimf(univ_glucose, [140, 180, 230]) 
            g['very_high']  = fuzzy.trapmf(univ_glucose, [200, 250, 400, 400]) 

            m['zero']     = fuzzy.trimf(univ_mult, [0.00, 0.00, 0.10])
            m['very_low'] = fuzzy.trimf(univ_mult, [0.05, 0.30, 0.70])
            m['low']      = fuzzy.trimf(univ_mult, [0.40, 0.80, 1.20])
            m['normal']   = fuzzy.trimf(univ_mult, [0.90, 1.30, 2.00])
            m['elevated'] = fuzzy.trimf(univ_mult, [1.50, 2.40, 3.20])
            m['high']     = fuzzy.trimf(univ_mult, [2.50, 3.50, 4.50]) 

            rules = [
                ctrl.Rule(g['severe_low'] | g['low'], m['zero']),
                ctrl.Rule((t['rapid_fall'] | t['falling']) & g['target'], m['zero']),
                ctrl.Rule(i['high'] & ~g['very_high'], m['zero']),
                ctrl.Rule(i['high'] & g['very_high'], m['very_low']), 
                ctrl.Rule(i['moderate'] & ~g['high'] & ~g['very_high'], m['very_low']),
                ctrl.Rule(g['very_high'] & t['rapid_rise'], m['high']),
                ctrl.Rule(g['very_high'] & ~i['high'], m['elevated']),
                ctrl.Rule(g['high'] & t['rising'] & ~i['high'], m['elevated']),
                ctrl.Rule(g['target'] & t['stable'] & i['very_low'], m['normal']),
                ctrl.Rule(g['high'] & t['falling'], m['low']),
            ]

        return ctrl.ControlSystemSimulation(ctrl.ControlSystem(rules))

    def reset(self):
        self.last_cgm = None
        self.cgm_history.clear()
        self.cho_history.clear()
        self.time_in_high = 0
        self.x = np.array([140.0, 0.0])
        self.P = np.eye(2) * 100.0
        self.iob_model.reset()

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

        raw_cgm = float(observation.CGM)
        now = info.get("time", datetime.now())
        meal_cho = info.get("meal", 0.0)
        basal_nominal = info.get("basal_nominal", 1.0)
        cr_nominal = info.get("CR", 8.0)

        self.cgm_history.append(raw_cgm)
        self.cho_history.append(meal_cho)

        filtered_cgm, _ = self._kalman_update(raw_cgm)
        poly_rate, predicted_cgm = self._calculate_poly_trend_and_prediction(horizon_mins=30.0)
        trend_clipped = np.clip(poly_rate, -12.0, 12.0)
        current_iob = np.clip(self.iob_model.calculate(), 0, 15)
        self.last_cgm = raw_cgm

        if filtered_cgm > self.high_threshold:
            self.time_in_high += self.sample_time
        else:
            self.time_in_high = max(0, self.time_in_high - self.sample_time * 2)

        # ------------------------------------------------------------------
        # ISOLATED SAFETY LAYERS
        # ------------------------------------------------------------------
        if self.patient_type == "normal":
            safe_max_iob = (basal_nominal * 2.5) + 0.3 
            iob_percent = np.clip((current_iob / safe_max_iob) * 100.0, 0, 100)
            
            try:
                self.flc.input['glucose'] = np.clip(filtered_cgm, 30, 400)
                self.flc.input['trend']   = trend_clipped
                self.flc.input['iob_pct'] = iob_percent
                self.flc.compute()
                final_basal_mult = float(self.flc.output['multiplier'])
            except Exception:
                final_basal_mult = 0.0 if filtered_cgm < 100 else 1.0

            if predicted_cgm < 125.0:
                final_basal_mult = 0.0
            elif filtered_cgm < 130.0 and poly_rate < -1.0:
                final_basal_mult = 0.0
            elif sum(self.cho_history) > 20 and poly_rate < -1.0:
                final_basal_mult = 0.0 

            if poly_rate < -1.0 and current_iob > (safe_max_iob * 0.25):
                final_basal_mult = 0.0

            if self.time_in_high > self.time_high_force_threshold and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 200.0:
                    final_basal_mult = max(final_basal_mult, 1.8)


        elif self.patient_type == "sensitive":
            safe_max_iob = (basal_nominal * 1.8) + 0.1 
            iob_percent = np.clip((current_iob / safe_max_iob) * 100.0, 0, 100)
            
            try:
                self.flc.input['glucose'] = np.clip(filtered_cgm, 30, 400)
                self.flc.input['trend']   = trend_clipped
                self.flc.input['iob_pct'] = iob_percent
                self.flc.compute()
                final_basal_mult = float(self.flc.output['multiplier'])
            except Exception:
                final_basal_mult = 0.0 if filtered_cgm < 120 else 1.0

            if current_iob > safe_max_iob and filtered_cgm < 220.0:
                final_basal_mult = 0.0

            if predicted_cgm < 140.0:
                final_basal_mult = 0.0
            elif filtered_cgm < 145.0 and poly_rate < -0.5:
                final_basal_mult = 0.0
            elif sum(self.cho_history) > 15 and poly_rate < 0.0: 
                final_basal_mult = 0.0 

            if poly_rate < -0.5 and current_iob > (safe_max_iob * 0.10):
                final_basal_mult = 0.0

            if self.time_in_high > self.time_high_force_threshold and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 230.0:
                    final_basal_mult = max(final_basal_mult, 1.2)


        elif self.patient_type == "resistant":
            safe_max_iob = (basal_nominal * 3.0) + 1.0 
            iob_percent = np.clip((current_iob / safe_max_iob) * 100.0, 0, 100)
            
            try:
                self.flc.input['glucose'] = np.clip(filtered_cgm, 30, 400)
                self.flc.input['trend']   = trend_clipped
                self.flc.input['iob_pct'] = iob_percent
                self.flc.compute()
                final_basal_mult = float(self.flc.output['multiplier'])
            except Exception:
                final_basal_mult = 0.0 if filtered_cgm < 100 else 1.0

            if predicted_cgm < 140.0: 
                final_basal_mult = 0.0
            elif filtered_cgm < 150.0 and poly_rate < -0.5: 
                final_basal_mult = 0.0
            elif sum(self.cho_history) > 20 and poly_rate < -0.5:
                final_basal_mult = 0.0 

            if poly_rate < -0.2 and current_iob > (safe_max_iob * 0.05):
                final_basal_mult = 0.0

            if filtered_cgm > 180.0 and poly_rate > 0.0:
                final_basal_mult = max(final_basal_mult, 2.0) 

            if self.time_in_high > 30 and current_iob < safe_max_iob:
                if len(self.cgm_history) >= 10 and np.mean(list(self.cgm_history)[-10:]) > 200.0:
                    if poly_rate > -0.1: 
                        final_basal_mult = max(final_basal_mult, 3.0) 

        # ------------------------------------------------------------------

        # Calculate standard basal step and meal bolus
        final_basal_mult = final_basal_mult * self.basal_adapt_factor
        basal_step = (basal_nominal * final_basal_mult / 60.0) * self.sample_time
        if meal_cho > 0:
            print("MEAL   :",meal_cho)
        bolus = (meal_cho / cr_nominal) if meal_cho > 0 else 0.0

        # ==================================================================
        # NEW: SUPER MICRO-BOLUS (SMB) LAYER
        # ==================================================================
        # 1. Higher trigger threshold (>180 instead of >170)
        if bolus == 0.0 and filtered_cgm > 1780.0 and poly_rate > 0.5:
            # 2. Stricter IOB Limit: Only fire if bucket is less than 30% full (was 60%)
            if current_iob < (safe_max_iob * 0.30):
                # 3. Slightly smaller burst (20 minutes of basal instead of 30)
                smb_dose = basal_nominal * 0.33 
                bolus += smb_dose
                
                # 4. MUTUAL EXCLUSION: If we give an SMB, shut off the basal drip for this cycle
                final_basal_mult = 0.0 
                
                # Tag it in the logs
                iob_model_log = f"SMB_{self.patient_type.upper()}"
            else:
                iob_model_log = f"ISO_FUZZY_{self.patient_type.upper()}"
        else:
            iob_model_log = f"ISO_FUZZY_{self.patient_type.upper()}"
        # ==================================================================
        # ==================================================================

        # The bolus variable now contains either the meal bolus OR the SMB dose.
        # This updates the tracker immediately, preventing rapid-fire stacking.
        self.iob_model.update(basal_step, bolus, self.sample_time)

        if self.logger:
            self.logger.log_step(
                step=info.get("step", 0),
                time=now,
                cgm=raw_cgm,
                basal=basal_nominal * final_basal_mult,
                bolus=bolus,
                iob=current_iob,
                iob_model=iob_model_log, # Will show "SMB_..." if fired
                cho=meal_cho,
                aggression=final_basal_mult,
                trend=poly_rate,
                target=120.0,
                filtered_cgm=filtered_cgm,
                basal_adapt=self.basal_adapt_factor
            )

        return Action(basal=basal_step, bolus=bolus)