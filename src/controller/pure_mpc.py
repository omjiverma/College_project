# src/controller/pure_mpc.py

# from __future__ import annotations

# import logging
# from datetime import datetime
# from typing import List, Tuple

# import numpy as np
# from simglucose.controller.base import Action, Controller

# from src.models.bergman import BergmanMinimalModel
# from src.models.iob import WalshIOB
# from src.utils.logging import PatientLogger


# class T1DControllerPureMPC(Controller):
#     """
#     Pure MPC-based controller (no PID/SMB heuristics).

#     Notes:
#       - Uses the Bergman Minimal Model for predictions.
#       - Solves a small receding-horizon optimization via coarse grid search.
#       - Insulin decision is an insulin rate (U/min), per simglucose conventions.
#     """

#     def __init__(self, profile: dict, logger: PatientLogger | None = None):
#         self.p = profile
#         self.logger = logger

#         # simglucose timestep is 3 minutes by default in this project
#         self.sample_time_min = float(self.p.get("sample_time_min", 3.0))

#         # Models/state
#         self.bergman = BergmanMinimalModel(profile)
#         self.dia_min = float(self.p.get("DIA", 300.0))
#         self.iob_model = WalshIOB(dia_minutes=self.dia_min)
#         self.iob = 0.0
#         self.glucose_hist: List[float] = []
#         self.disturbance_est = 0.0  # mg/dL/min (offset-free MPC)

#         # MPC configuration
#         self.internal_model = "bergman"#str(self.p.get("mpc_internal_model", "iob_linear")).strip().lower()
#         self.target = float(self.p.get("target", 110.0))
#         horizon_min = float(self.p.get("mpc_horizon", 30.0))
#         self.horizon_steps = max(3, int(round(horizon_min / self.sample_time_min)))

#         self.CR = float(self.p.get("CR", 30.0))
#         self.CF = float(self.p.get("CF_base", self.p.get("CF", 50.0)))

#         # Insulin limits are in U/min
#         basal_nominal_u_hr = float(self.p.get("basal_nominal", 0.0))
#         self.u_ref = basal_nominal_u_hr / 60.0

#         default_u_max = max(0.05, 10.0 * float(self.u_ref)) if self.u_ref > 0 else 0.05
#         self.u_max = float(self.p.get("mpc_u_max", default_u_max))
#         u_cap = float(self.p.get("mpc_u_max_cap", 0.2))
#         if u_cap > 0:
#             self.u_max = float(min(self.u_max, u_cap))

#         self.grid_u0 = int(self.p.get("mpc_grid_u0", 21))
#         self.grid_usteady = int(self.p.get("mpc_grid_usteady", 21))

#         self.w_g = float(self.p.get("mpc_w_g", 1.0))
#         self.w_u = float(self.p.get("mpc_w_u", 0.002))
#         self.w_du = float(self.p.get("mpc_w_du", 0.02))
#         self.w_hypo = float(self.p.get("mpc_w_hypo", 50.0))

#         self.hypo_soft = float(self.p.get("mpc_hypo_soft", 80.0))
#         self.suspend_below = float(self.p.get("mpc_suspend_below", 70.0))
#         self.preemptive_suspend_below = float(self.p.get("mpc_preemptive_suspend_below", 90.0))

#         # Offset-free MPC disturbance estimation (helps match simglucose dynamics)
#         self.use_disturbance_est = bool(self.p.get("mpc_disturbance_est", True))
#         self.disturbance_alpha = float(self.p.get("mpc_disturbance_alpha", 0.3))
#         self.disturbance_clip = float(self.p.get("mpc_disturbance_clip", 20.0))

#         # IOB-linear internal model (more robust defaults for simglucose)
#         default_tau = max(30.0, float(self.dia_min) / 3.0)
#         self.iob_tau_min = float(self.p.get("mpc_iob_tau_min", default_tau))
#         self.iob_gain = float(self.p.get("mpc_iob_gain", self.CF / max(float(self.dia_min), 1e-6)))

#     def reset(self):
#         """Reset controller internal state for a new episode."""
#         self.bergman.reset()
#         self.iob_model.reset()
#         self.iob = 0.0
#         self.glucose_hist.clear()
#         self.disturbance_est = 0.0

#     def _trend_mgdl_per_min(self) -> float:
#         if len(self.glucose_hist) < 6:
#             return 0.0
#         recent = np.array(self.glucose_hist[-10:], dtype=float)
#         x = np.arange(len(recent), dtype=float) * float(self.sample_time_min)
#         try:
#             slope = np.polyfit(x, recent, 1)[0]
#             return float(slope)
#         except Exception:
#             return 0.0

#     def _meal_disturbance_mgdl_min(self, cho_g: float) -> float:
#         """
#         Map CHO (g) into a simple constant glucose appearance disturbance D (mg/dL/min).

#         Uses a coarse approximation:
#           - Meal raises BG by ~ (CHO / CR) * CF  over the full absorption window.
#           - Spread uniformly over `meal_absorption_minutes`.
#         """
#         if cho_g <= 0.0:
#             return 0.0

#         absorption = float(self.p.get("meal_absorption_minutes", 60.0))

#         CR = max(float(self.CR), 1e-6)
#         absorption = max(absorption, 1e-6)
#         delta_bg = cho_g * (float(self.CF) / CR)
#         return float(delta_bg / absorption)

#     def _update_disturbance_est(self, cgm: float, trend_mgdl_min: float, D_meal: float) -> float:
#         """
#         Estimate an additive disturbance term (mg/dL/min) so the internal model's
#         instantaneous slope matches the observed CGM trend (offset-free MPC).
#         """
#         if not self.use_disturbance_est:
#             self.disturbance_est = 0.0
#             return 0.0

#         if self.internal_model == "bergman":
#             X0, _ = self.bergman.get_states()
#             p1 = float(self.bergman.p1)
#             Gb = float(self.bergman.Gb)
#             # Bergman: dG = -(p1 + X) * G + p1*Gb + D_meal + d
#             dG_nominal = (-(p1 + float(X0)) * float(cgm)) + (p1 * Gb) + float(D_meal)
#             d_needed = float(trend_mgdl_min) - float(dG_nominal)
#         else:
#             # IOB-linear: dG = D_meal + d - k*IOB
#             dG_nominal = float(D_meal) - float(self.iob_gain) * float(self.iob)
#             d_needed = float(trend_mgdl_min) - float(dG_nominal)

#         clip = max(0.0, float(self.disturbance_clip))
#         if clip > 0.0:
#             d_needed = float(np.clip(d_needed, -clip, clip))

#         a = float(np.clip(self.disturbance_alpha, 0.0, 1.0))
#         self.disturbance_est = (1.0 - a) * float(self.disturbance_est) + a * d_needed
#         return float(self.disturbance_est)

#     def _effective_u_max(self) -> float:
#         """
#         Reduce max insulin delivery when IOB is high (soft safety constraint).
#         """
#         iob_thr = float(self.p.get("iob_mpc_threshold", 3.0))
#         if self.iob <= iob_thr:
#             return self.u_max

#         # Conservative clamp when stacking insulin
#         scale = max(0.2, 1.0 - 0.2 * (self.iob - iob_thr))
#         return float(self.u_max * scale)

#     def _rollout_cost(
#         self,
#         G0: float,
#         X0: float,
#         I0: float,
#         D: float,
#         u0: float,
#         usteady: float,
#     ) -> Tuple[float, float]:
#         """
#         Rollout Bergman predictions and compute MPC objective.

#         Returns:
#             (cost, final_predicted_bg)
#         """
#         G = float(G0)
#         X = float(X0)
#         I = float(I0)
#         cost = 0.0
#         u_prev = float(self.u_ref)
#         dt = float(self.sample_time_min)

#         for k in range(self.horizon_steps):
#             u = float(u0 if k == 0 else usteady)
#             G, X, I = self.bergman.step(G, X, I, U=u, D=D, dt=dt)

#             err = G - self.target
#             cost += self.w_g * (err * err)
#             cost += self.w_u * ((u - self.u_ref) * (u - self.u_ref))
#             cost += self.w_du * ((u - u_prev) * (u - u_prev))
#             u_prev = u

#             if G < self.hypo_soft:
#                 d = self.hypo_soft - G
#                 cost += self.w_hypo * (d * d)

#         return float(cost), float(G)

#     def _rollout_cost_iob_linear(
#         self,
#         G0: float,
#         iob0: float,
#         D: float,
#         u0: float,
#         usteady: float,
#     ) -> Tuple[float, float]:
#         """
#         Simple predictive model:
#           IOB_{k+1} = IOB_k * exp(-dt/tau) + u_k
#           G_{k+1}   = G_k + dt * ( D - k_iob * IOB_k )
#         """
#         G = float(G0)
#         iob = float(iob0)
#         dt = float(self.sample_time_min)
#         tau = max(1e-6, float(self.iob_tau_min))
#         decay = float(np.exp(-dt / tau))

#         cost = 0.0
#         u_prev = float(self.u_ref)

#         for k in range(self.horizon_steps):
#             u = float(u0 if k == 0 else usteady)

#             # Update IOB and glucose
#             iob = iob * decay + (u * dt)
#             dG = float(D) - float(self.iob_gain) * iob
#             G = G + dt * dG

#             err = G - self.target
#             cost += self.w_g * (err * err)
#             cost += self.w_u * ((u - self.u_ref) * (u - self.u_ref))
#             cost += self.w_du * ((u - u_prev) * (u - u_prev))
#             u_prev = u

#             if G < self.hypo_soft:
#                 d = self.hypo_soft - G
#                 cost += self.w_hypo * (d * d)

#         return float(cost), float(G)

#     def _solve_mpc(self, cgm: float, cho_g: float) -> Tuple[float, float, float]:
#         """
#         Solve a small MPC problem by grid search over (u0, usteady).

#         Returns:
#             (u_apply, usteady_best, predicted_final_bg)
#         """
#         u_max = self._effective_u_max()
#         u_max = max(0.0, float(u_max))
#         if u_max <= 1e-8:
#             return 0.0, 0.0, float(cgm)

#         D_meal = self._meal_disturbance_mgdl_min(float(cho_g))
#         # Total disturbance used across the horizon
#         D = float(D_meal + float(self.disturbance_est))

#         u0_grid = np.linspace(0.0, u_max, max(2, self.grid_u0))
#         us_grid = np.linspace(0.0, u_max, max(2, self.grid_usteady))

#         best_cost = float("inf")
#         best_u0 = 0.0
#         best_us = 0.0
#         best_final = float(cgm)

#         if self.internal_model == "bergman":
#             X0, I0 = self.bergman.get_states()
#             for u0 in u0_grid:
#                 for us in us_grid:
#                     c, final_bg = self._rollout_cost(cgm, X0, I0, D, float(u0), float(us))
#                     if c < best_cost:
#                         best_cost = c
#                         best_u0 = float(u0)
#                         best_us = float(us)
#                         best_final = float(final_bg)
#         else:
#             for u0 in u0_grid:
#                 for us in us_grid:
#                     c, final_bg = self._rollout_cost_iob_linear(cgm, float(self.iob), D, float(u0), float(us))
#                     if c < best_cost:
#                         best_cost = c
#                         best_u0 = float(u0)
#                         best_us = float(us)
#                         best_final = float(final_bg)

#         return best_u0, best_us, best_final

#     def policy(self, observation, reward=None, done=None, **info):
#         # simglucose provides sample_time (minutes) and meal as g/min
#         dt = float(info.get("sample_time", self.sample_time_min))
#         if dt > 0:
#             self.sample_time_min = dt

#         if info.get("new_episode", False):
#             self.reset()
#             if self.logger:
#                 try:
#                     self.logger.__init__(self.logger.patient_name)
#                 except Exception:
#                     pass
#             logging.info("Pure MPC controller reset for new episode.")

#         cgm = float(observation.CGM)
#         meal_rate_g_min = float(info.get("meal", 0.0))
#         cho = meal_rate_g_min * dt  # convert to grams over this control interval
#         time = info.get("time", datetime.now())
#         step = int(info.get("step", 0))

#         self.glucose_hist.append(cgm)
#         if len(self.glucose_hist) > 500:
#             self.glucose_hist.pop(0)

#         trend = self._trend_mgdl_per_min()
#         D_meal = self._meal_disturbance_mgdl_min(cho)
#         self._update_disturbance_est(cgm=cgm, trend_mgdl_min=trend, D_meal=D_meal)

#         # Hard safety: suspend insulin if already low
#         if cgm < self.suspend_below or (cgm < self.preemptive_suspend_below and trend < 0.0):
#             basal = 0.0
#             bolus = 0.0
#             predicted_final = float(cgm)
#             usteady = 0.0
#             mpc_used = False
#         else:
#             basal, usteady, predicted_final = self._solve_mpc(cgm, cho)
#             bolus = 0.0
#             mpc_used = True

#         # Update internal prediction states with applied insulin (receding horizon)
#         if self.internal_model == "bergman":
#             try:
#                 D0 = float(D_meal + float(self.disturbance_est))
#                 _, X1, I1 = self.bergman.step(
#                     cgm,
#                     self.bergman.X_state,
#                     self.bergman.I_state,
#                     U=basal,
#                     D=D0,
#                     dt=dt,
#                 )
#                 self.bergman.update_states(X1, I1)
#             except Exception:
#                 pass

#         # Update IOB
#         self.iob_model.update(basal_u=float(basal) * dt, bolus_u=float(bolus) * dt, dt_min=dt)
#         self.iob = float(self.iob_model.calculate())

#         if self.logger:
#             try:
#                 # Controller action is U/min; logger expects U/hr
#                 basal_u_hr = float(basal) * 60.0
#                 self.logger.log_step(
#                     step=step,
#                     time=time,
#                     cgm=cgm,
#                     basal=basal_u_hr,
#                     bolus=bolus,
#                     iob=self.iob,
#                     iob_model="PURE_MPC_BERGMAN",
#                     cho=cho,
#                     aggression=1.0,
#                     trend=trend,
#                     target=self.target,
#                     mpc_used=int(bool(mpc_used)),
#                     mpc_u0=round(float(basal), 6),
#                     mpc_usteady=round(float(usteady), 6),
#                     mpc_predicted_final_bg=round(float(predicted_final), 2),
#                     mpc_disturbance_est=round(float(self.disturbance_est), 5),
#                     mpc_internal_model=self.internal_model,
#                 )
#             except Exception:
#                 logging.exception("Controller logging failed.")

#         return Action(basal=float(basal), bolus=float(bolus))
# from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Tuple

import numpy as np
from simglucose.controller.base import Action, Controller

from src.models.bergman import BergmanMinimalModel
from src.models.iob import WalshIOB
from src.utils.logging import PatientLogger

import logging
from datetime import datetime
from typing import List, Tuple

import numpy as np
from simglucose.controller.base import Action, Controller

from src.models.bergman import BergmanMinimalModel
from src.models.iob import WalshIOB
from src.utils.logging import PatientLogger


class T1DControllerHybridMPC(Controller):

    # ============================================================
    # INITIALIZATION
    # ============================================================

    def __init__(self, profile: dict, logger: PatientLogger | None = None):
        self.p = profile
        self.logger = logger

        # --- Timing ---
        self.sample_time_min = float(self.p.get("sample_time_min", 3.0))

        # --- Internal Model ---
        self.bergman = BergmanMinimalModel(profile)
        self.dia_min = float(self.p.get("DIA", 300.0))
        self.iob_model = WalshIOB(dia_minutes=self.dia_min)

        # --- State ---
        self.iob = 0.0
        self.glucose_hist: List[float] = []

        # --- MPC Configuration ---
        self.target = float(self.p.get("target", 90.0))
        horizon_min = float(self.p.get("mpc_horizon", 120.0))
        self.horizon_steps = max(10, int(round(horizon_min / self.sample_time_min)))

        self.CF = float(self.p.get("CF", 50.0))
        self.CR = float(self.p.get("CR", 30.0))

        # Basal reference (U/min)
        basal_nominal_u_hr = float(self.p.get("basal_nominal", 0.0))
        self.u_ref = basal_nominal_u_hr / 60.0

        self.u_max = float(self.p.get("mpc_u_max_cap", 0.5))
        self.grid_size = int(self.p.get("mpc_grid_size", 15))

        # --- Cost Weights ---
        self.w_g_hyper = float(self.p.get("mpc_w_g_hyper", 4.0))
        self.w_g_hypo = float(self.p.get("mpc_w_g_hypo", 2.0))
        self.w_u = float(self.p.get("mpc_w_u", 0.0005))
        self.w_du = float(self.p.get("mpc_w_du", 0.005))
        self.w_hypo = float(self.p.get("mpc_w_hypo", 120.0))
        self.w_terminal = float(self.p.get("mpc_w_terminal", 15.0))

        # --- Safety ---
        self.hypo_soft = float(self.p.get("mpc_hypo_soft", 80.0))
        self.suspend_below = float(self.p.get("mpc_suspend_below", 70.0))
        self.preemptive_suspend_below = float(self.p.get("mpc_preemptive_suspend_below", 90.0))

        # --- Bolus ---
        self.max_bolus = float(self.p.get("max_bolus", 5.0))

    # ============================================================
    # RESET
    # ============================================================

    def reset(self):
        self.bergman.reset()
        self.iob_model.reset()
        self.iob = 0.0
        self.glucose_hist.clear()

    # ============================================================
    # TREND ESTIMATION
    # ============================================================

    def _trend_mgdl_per_min(self) -> float:
        if len(self.glucose_hist) < 6:
            return 0.0
        recent = np.array(self.glucose_hist[-10:], dtype=float)
        x = np.arange(len(recent), dtype=float) * self.sample_time_min
        try:
            return float(np.polyfit(x, recent, 1)[0])
        except Exception:
            return 0.0

    # ============================================================
    # MEAL DISTURBANCE
    # ============================================================

    def _meal_disturbance(self, cho_g: float) -> float:
        if cho_g <= 0:
            return 0.0
        absorption = float(self.p.get("meal_absorption_minutes", 60.0))
        delta_bg = cho_g * (self.CF / self.CR)
        return delta_bg / absorption

    # ============================================================
    # INSULIN STACKING PROTECTION
    # ============================================================

    def _effective_u_max(self):
        if self.iob < 3.0:
            return self.u_max
        return self.u_max * 0.5

    # ============================================================
    # CORRECTION BOLUS
    # ============================================================

    def _compute_correction_bolus(self, cgm: float) -> float:
        if cgm <= self.target:
            return 0.0

        raw = (cgm - self.target) / self.CF
        stacking_protection = 0.5 * self.iob
        bolus = max(0.0, raw - stacking_protection)
        return float(min(bolus, self.max_bolus))

    # ============================================================
    # MPC ROLLOUT
    # ============================================================

    def _rollout(self, G0, X0, I0, D, u0, usteady):

        G = float(G0)
        X = float(X0)
        I = float(I0)

        cost = 0.0
        u_prev = self.u_ref
        dt = self.sample_time_min

        for k in range(self.horizon_steps):

            u = u0 if k == 0 else usteady
            G, X, I = self.bergman.step(G, X, I, U=u, D=D, dt=dt)

            err = G - self.target

            if G > self.target:
                cost += self.w_g_hyper * err * err
            else:
                cost += self.w_g_hypo * err * err

            cost += self.w_u * (u - self.u_ref) ** 2
            cost += self.w_du * (u - u_prev) ** 2

            if G < self.hypo_soft:
                cost += self.w_hypo * (self.hypo_soft - G) ** 2

            if k == self.horizon_steps - 1:
                cost += self.w_terminal * err * err

            u_prev = u

        return cost, G

    # ============================================================
    # SOLVE MPC
    # ============================================================

    def _solve_mpc(self, cgm, cho):

        D = self._meal_disturbance(cho)
        X0, I0 = self.bergman.get_states()

        u_max = self._effective_u_max()
        grid = np.linspace(0.0, u_max, self.grid_size)

        best_cost = float("inf")
        best_u0 = 0.0
        best_us = 0.0
        best_final = cgm

        for u0 in grid:
            for us in grid:
                c, final_bg = self._rollout(cgm, X0, I0, D, u0, us)
                if c < best_cost:
                    best_cost = c
                    best_u0 = u0
                    best_us = us
                    best_final = final_bg

        return best_u0, best_us, best_final

    # ============================================================
    # POLICY
    # ============================================================

    def policy(self, observation, reward=None, done=None, **info):

        dt = float(info.get("sample_time", self.sample_time_min))
        self.sample_time_min = dt

        if info.get("new_episode", False):
            self.reset()

        cgm = float(observation.CGM)
        meal_rate = float(info.get("meal", 0.0))
        cho = meal_rate * dt

        # Update history
        self.glucose_hist.append(cgm)
        if len(self.glucose_hist) > 500:
            self.glucose_hist.pop(0)

        trend = self._trend_mgdl_per_min()

        # =========================
        # SAFETY SUPERVISOR
        # =========================

        if cgm < self.suspend_below:
            return Action(basal=0.0, bolus=0.0)

        if cgm < self.preemptive_suspend_below and trend < 0:
            return Action(basal=0.0, bolus=0.0)

        # =========================
        # MPC BASAL
        # =========================

        basal, usteady, predicted = self._solve_mpc(cgm, cho)

        # =========================
        # CORRECTION BOLUS
        # =========================

        bolus = self._compute_correction_bolus(cgm)

        basal = float(np.clip(basal, 0.0, self._effective_u_max()))
        bolus = float(np.clip(bolus, 0.0, self.max_bolus))

        # =========================
        # Update internal states
        # =========================

        try:
            D0 = self._meal_disturbance(cho)
            _, X1, I1 = self.bergman.step(
                cgm,
                self.bergman.X_state,
                self.bergman.I_state,
                U=basal,
                D=D0,
                dt=dt,
            )
            self.bergman.update_states(X1, I1)
        except Exception:
            pass

        self.iob_model.update(
            basal_u=basal * dt,
            bolus_u=bolus,
            dt_min=dt
        )
        self.iob = float(self.iob_model.calculate())

        # =========================
        # LOGGING
        # =========================

        if self.logger:
            try:
                basal_u_hr = basal * 60.0
                self.logger.log_step(
                    step=int(info.get("step", 0)),
                    time=info.get("time", datetime.now()),
                    cgm=cgm,
                    basal=basal_u_hr,
                    bolus=bolus,
                    iob=self.iob,
                    iob_model="HYBRID_MPC_BERGMAN",
                    cho=cho,
                    aggression=1.0,
                    trend=trend,
                    target=self.target,
                    mpc_used=1,
                    mpc_u0=round(float(basal), 6),
                    mpc_usteady=round(float(usteady), 6),
                    mpc_predicted_final_bg=round(float(predicted), 2),
                )
            except Exception:
                logging.exception("Controller logging failed.")

        return Action(basal=basal, bolus=bolus)