#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import replace

try:
    import optuna
except ImportError:
    raise SystemExit("pip install optuna")

from futbol_live_betting_probabilities import PARAMETER_PRESETS, ModelParams
from backtest_metrics import run_history_metrics


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="live_history")
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--preset", default="balanced")
    args = ap.parse_args()
    base: ModelParams = PARAMETER_PRESETS[args.preset]

    def objective(trial: optuna.Trial) -> float:
        p = replace(
            base,
            wpf_high=trial.suggest_float("wpf_high", 0.55, 0.85),
            wpf_low=trial.suggest_float("wpf_low", 0.15, 0.45),
            siege_touch_threshold=trial.suggest_float("siege_touch", 6.0, 14.0),
            siege_expm1_scale=trial.suggest_float("siege_expm1", 0.06, 0.18),
            siege_boost_cap=trial.suggest_float("siege_cap", 0.25, 0.60),
            corner_nb_theta=trial.suggest_float("corner_nb_theta", 6.0, 24.0),
            card_nb_theta=trial.suggest_float("card_nb_theta", 4.0, 20.0),
            ev_p25_min=trial.suggest_float("ev_p25_min", 1.0, 1.06),
        )
        m = run_history_metrics(p, args.dir, 10)
        trial.set_user_attr("brier", m["brier"])
        trial.set_user_attr("n_bets", m["n_bets"])
        if m["n_bets"] < 5:
            return -1e6
        return m["sharpe"]

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=args.trials)
    print("Best Sharpe:", study.best_value, study.best_params)
    print("Brier @ best:", study.best_trial.user_attrs.get("brier"))


if __name__ == "__main__":
    main()
