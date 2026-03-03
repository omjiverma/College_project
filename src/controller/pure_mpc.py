from __future__ import annotations
import logging
from datetime import datetime
from typing import List

import numpy as np
from simglucose.controller.base import Action, Controller

from src.models.bergman import BergmanMinimalModel
from src.models.iob import WalshIOB
from src.utils.logging import PatientLogger


class T1DControllerHybridMPCDynamic(Controller):

    # ============================================================
    # INITIALIZATION
    # ============================================================

    def __init__(self, profile: dict, logger: PatientLogger | None = None):

        self.p = profile
        self.logger = logger

        self.sample_time_min = float(self.p.get("sample_time_min", 3.0))

        # Internal model
        self.bergman = BergmanMinimalModel(profile)
        self.iob_model = WalshIOB(dia_minutes=float(self.p.get("DIA", 300)))

        # State
        self.iob = 0.0
        self.glucose_hist: List[float] = []

        # Core parameters
        self.target = float(self.p.get("target", 110.0))
        horizon_min = float(self.p.get("mpc_horizon", 120.0))
        self.horizon_steps = max(10, int(round(horizon_min / self.sample_time_min)))

        self.CF = float(self.p.get("CF_base", self.p.get("CF", 50.0)))
        self.CR = float(self.p.get("CR", 30.0))

        basal_nominal = float(self.p.get("basal_nominal", 1.0))
        self.u_ref = basal_nominal / 60.0  # U/min
        print(f"basal_nominal={basal_nominal}U/hr → u_ref={self.u_ref:.4f}U/min")

        self.u_max = float(self.p.get("mpc_u_max_cap", 0.6))
        self.grid_size = int(self.p.get("mpc_grid_size", 15))
        self.max_bolus = float(self.p.get("max_bolus", 6.0))

        # Cost weights
        self.w_g_hyper = float(self.p.get("mpc_w_g_hyper", 4.0))
        self.w_g_hypo = float(self.p.get("mpc_w_g_hypo", 2.0))
        self.w_u = float(self.p.get("mpc_w_u", 0.003))
        self.w_du = float(self.p.get("mpc_w_du", 0.005))
        self.w_hypo = float(self.p.get("mpc_w_hypo", 120.0))
        self.w_terminal = float(self.p.get("mpc_w_terminal", 12.0))

        # Safety
        self.hypo_soft = float(self.p.get("mpc_hypo_soft", 80.0))
        self.suspend_below = float(self.p.get("mpc_suspend_below", 70.0))
        self.preemptive_suspend_below = float(
            self.p.get("mpc_preemptive_suspend_below", 90.0)
        )

    # ============================================================
    # TREND
    # ============================================================

    def _trend(self):
        if len(self.glucose_hist) < 6:
            return 0.0
        recent = np.array(self.glucose_hist[-10:])
        x = np.arange(len(recent)) * self.sample_time_min
        try:
            return float(np.polyfit(x, recent, 1)[0])
        except Exception:
            return 0.0

    # ============================================================
    # MEAL DISTURBANCE
    # ============================================================

    def _meal_disturbance(self, cho):
        if cho <= 0:
            return 0.0
        absorption = float(self.p.get("meal_absorption_minutes", 60))
        delta_bg = cho * (self.CF / self.CR)
        return delta_bg / absorption

    # ============================================================
    # DYNAMIC U_MAX
    # ============================================================

    def _dynamic_u_max(self, cgm):
        u = self.u_max

        if cgm > 200:
            u *= 1.3
        if cgm > 250:
            u *= 1.6

        if self.iob > 3.0:
            u *= 0.6

        return u

    # ============================================================
    # DYNAMIC CORRECTION BOLUS
    # ============================================================

    def _correction_bolus(self, cgm):

        if cgm <= self.target:
            return 0.0

        raw = (cgm - self.target) / self.CF

        # Dynamic boost
        if cgm > 200:
            raw *= 1.3
        if cgm > 250:
            raw *= 1.5

        # IOB stacking protection
        raw -= 0.4 * self.iob

        return float(np.clip(raw, 0.0, self.max_bolus))

    # ============================================================
    # MPC ROLLOUT
    # ============================================================

    def _rollout(self, G0, X0, I0, D, u0, usteady):

        G, X, I = G0, X0, I0
        cost = 0.0
        u_prev = self.u_ref
        dt = self.sample_time_min

        for k in range(self.horizon_steps):

            u = u0 if k == 0 else usteady
            G, X, I = self.bergman.step(G, X, I, U=u, D=D, dt=dt)

            err = G - self.target

            if G > self.target:
                hyper_w = self.w_g_hyper

                # Hyper-weight scaling
                if G > 200:
                    hyper_w *= 1.5
                if G > 250:
                    hyper_w *= 2.0

                cost += hyper_w * err * err
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

        u_max = self._dynamic_u_max(cgm)
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
    def reset(self):
        """
        Reset controller internal state for new simulation episode.
        """
        self.bergman.reset()
        self.iob_model.reset()
        self.iob = 0.0
        self.glucose_hist.clear()

    def policy(self, observation, reward=None, done=None, **info):

        dt = float(info.get("sample_time", self.sample_time_min))
        self.sample_time_min = dt

        cgm = float(observation.CGM)
        meal_rate = float(info.get("meal", 0.0))
        cho = meal_rate * dt

        # Update glucose history
        self.glucose_hist.append(cgm)
        if len(self.glucose_hist) > 500:
            self.glucose_hist.pop(0)

        trend = self._trend()

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

        bolus = self._correction_bolus(cgm)

        basal = float(np.clip(basal, 0.0, self._dynamic_u_max(cgm)))
        bolus = float(np.clip(bolus, 0.0, self.max_bolus))

        # Update internal model state
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

        # Update IOB
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
                self.logger.log_step(
                    step=int(info.get("step", 0)),
                    time=info.get("time", datetime.now()),
                    cgm=cgm,
                    basal=basal * 60.0,
                    bolus=bolus,
                    iob=self.iob,
                    iob_model="HYBRID_MPC_DYNAMIC",
                    cho=cho,
                    aggression=1.0,
                    trend=trend,
                    target=self.target,
                    mpc_used=1,
                    mpc_u0=round(basal, 6),
                    mpc_usteady=round(usteady, 6),
                    mpc_predicted_final_bg=round(predicted, 2),
                )
            except Exception:
                logging.exception("Logging failed")


        return Action(basal=basal, bolus=bolus)