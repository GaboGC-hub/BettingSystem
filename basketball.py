#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np


FEATURE_COLUMNS = [
    "minute",
    "time_remaining",
    "score_diff",
    "abs_score_diff",
    "pregame_edge",
    "recent_run",
    "pace_index",
    "home_points_per_min",
    "away_points_per_min",
    "foul_pressure",
    "clutch_time",
    "garbage_time",
    "comeback_pressure",
]


@dataclass(frozen=True)
class BacktestConfig:
    bankroll_start: float = 1000.0
    min_minute: int = 6
    max_minute: int = 46
    edge_threshold: float = 0.05
    kelly_fraction: float = 0.08
    max_stake_fraction: float = 0.008
    min_stake_fraction: float = 0.003


@dataclass(frozen=True)
class ModelSummary:
    mean: np.ndarray
    std: np.ndarray
    weights: np.ndarray
    bias: float


@dataclass(frozen=True)
class LiveDecision:
    side: str
    edge: float
    odds: float
    prob_model: float
    prob_market: float
    stake_fraction: float
    quality: float
    phase: str
    minute: float


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -25.0, 25.0)))


def normal_cdf(value: float) -> float:
    return float(0.5 * (1.0 + math.erf(value / np.sqrt(2.0))))


def infer_phase(minute: int, score_diff: float, recent_run: float, clutch_time: int, garbage_time: int) -> str:
    if clutch_time:
        return "clutch"
    if garbage_time:
        return "garbage"
    if abs(score_diff) <= 6 and minute >= 30:
        return "coin_flip"
    if abs(recent_run) >= 6:
        return "momentum"
    if abs(score_diff) >= 12:
        return "control"
    return "normal"


def generate_synthetic_games(n_games: int = 800, seed: int = 7) -> list[dict[str, float | int | str]]:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, float | int | str]] = []

    for game_id in range(n_games):
        pregame_edge = float(rng.normal(0.0, 6.0))
        pace_factor = clamp(float(rng.normal(1.0, 0.10)), 0.78, 1.25)
        volatility = clamp(float(rng.normal(1.0, 0.14)), 0.75, 1.35)
        foul_heat = clamp(float(rng.normal(1.0, 0.14)), 0.70, 1.35)
        hidden_endgame_edge = float(rng.normal(0.0, 2.0))
        era_drift = (game_id / max(1, n_games - 1)) - 0.5
        home_score = 0
        away_score = 0
        recent_nets: list[int] = []
        game_rows: list[dict[str, float | int | str]] = []

        for minute in range(1, 49):
            time_remaining = 48 - minute
            current_diff = home_score - away_score
            clutch_time = int(time_remaining <= 6 and abs(current_diff) <= 8)
            garbage_time = int(time_remaining <= 8 and abs(current_diff) >= 15)
            recent_run = float(sum(recent_nets[-3:]))

            base_home_rate = 2.35 * pace_factor + (pregame_edge * 0.05)
            base_away_rate = 2.35 * pace_factor - (pregame_edge * 0.05)

            if clutch_time and current_diff < 0:
                base_home_rate *= 1.12 + max(0.0, era_drift) * 0.08
            if clutch_time and current_diff > 0:
                base_away_rate *= 1.12 + max(0.0, era_drift) * 0.08
            if garbage_time and current_diff > 0:
                base_home_rate *= 0.90
                base_away_rate *= 0.96
            if garbage_time and current_diff < 0:
                base_home_rate *= 0.96
                base_away_rate *= 0.90

            if clutch_time:
                base_home_rate += hidden_endgame_edge * 0.05
                base_away_rate -= hidden_endgame_edge * 0.05

            run_adjustment = clamp(recent_run, -8.0, 8.0) * 0.05
            home_rate = clamp(base_home_rate + run_adjustment + rng.normal(0.0, 0.18 * volatility), 0.40, 5.80)
            away_rate = clamp(base_away_rate - run_adjustment + rng.normal(0.0, 0.18 * volatility), 0.40, 5.80)

            home_points = int(rng.poisson(home_rate))
            away_points = int(rng.poisson(away_rate))
            home_score += home_points
            away_score += away_points
            recent_nets.append(home_points - away_points)

            score_diff = home_score - away_score
            total_points = home_score + away_score
            pace_index = total_points / minute
            home_points_per_min = home_score / minute
            away_points_per_min = away_score / minute
            foul_pressure = clamp(
                foul_heat * (1.0 + 0.22 * clutch_time + 0.08 * (minute / 48.0)) + rng.normal(0.0, 0.08),
                0.55,
                1.85,
            )

            leverage_diff = score_diff / max(3.0, time_remaining + 2.0) * 10.0
            comeback_pressure = max(0.0, -score_diff) / max(4.0, time_remaining + 3.0)
            home_eff_delta = home_points_per_min - away_points_per_min
            remaining_minutes = max(1.0, 48.0 - minute)
            expected_margin = (
                score_diff
                + ((home_rate - away_rate) * remaining_minutes)
                + hidden_endgame_edge * (0.80 if clutch_time else 0.25)
                + pregame_edge * 0.08
            )
            variance_margin = max(
                1.0,
                (home_rate + away_rate) * remaining_minutes * (1.35 + 0.12 * volatility),
            )
            market_z = (expected_margin / np.sqrt(variance_margin)) + float(rng.normal(0.0, 0.05))
            prob_home_market = clamp(normal_cdf(market_z), 0.03, 0.97)
            vig = float(rng.uniform(0.095, 0.125) + max(0.0, era_drift) * 0.015)
            home_odds = max(1.05, 1.0 / (prob_home_market * (1.0 + vig / 2.0)))
            away_odds = max(1.05, 1.0 / ((1.0 - prob_home_market) * (1.0 + vig / 2.0)))

            game_rows.append(
                {
                    "game_id": game_id,
                    "minute": minute,
                    "time_remaining": time_remaining,
                    "home_score": home_score,
                    "away_score": away_score,
                    "score_diff": score_diff,
                    "abs_score_diff": abs(score_diff),
                    "pregame_edge": pregame_edge,
                    "recent_run": recent_run,
                    "pace_index": pace_index,
                    "home_points_per_min": home_points_per_min,
                    "away_points_per_min": away_points_per_min,
                    "foul_pressure": foul_pressure,
                    "clutch_time": clutch_time,
                    "garbage_time": garbage_time,
                    "comeback_pressure": comeback_pressure,
                    "phase": infer_phase(minute, score_diff, recent_run, clutch_time, garbage_time),
                    "home_odds": round(home_odds, 2),
                    "away_odds": round(away_odds, 2),
                }
            )

        while home_score == away_score:
            home_score += int(rng.poisson(5.5 + max(0.0, pregame_edge * 0.08)))
            away_score += int(rng.poisson(5.5 + max(0.0, -pregame_edge * 0.08)))

        home_win = int(home_score > away_score)
        for row in game_rows:
            row["home_win"] = home_win
            row["final_home_score"] = home_score
            row["final_away_score"] = away_score
        rows.extend(game_rows)

    return rows


def split_games(rows: list[dict[str, float | int | str]], train_ratio: float = 0.70) -> tuple[list[dict[str, float | int | str]], list[dict[str, float | int | str]]]:
    game_ids = sorted({int(row["game_id"]) for row in rows})
    split_idx = int(len(game_ids) * train_ratio)
    train_games = set(game_ids[:split_idx])
    train_rows = [row for row in rows if int(row["game_id"]) in train_games]
    test_rows = [row for row in rows if int(row["game_id"]) not in train_games]
    return train_rows, test_rows


def build_matrix(rows: list[dict[str, float | int | str]]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([[float(row[col]) for col in FEATURE_COLUMNS] for row in rows], dtype=float)
    y = np.array([float(row["home_win"]) for row in rows], dtype=float)
    return X, y


def train_model(rows: list[dict[str, float | int | str]], epochs: int = 1800, learning_rate: float = 0.06, l2: float = 0.002) -> ModelSummary:
    X, y = build_matrix(rows)
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    Xs = (X - mean) / std

    weights = np.zeros(Xs.shape[1], dtype=float)
    bias = 0.0

    for _ in range(epochs):
        logits = Xs @ weights + bias
        probs = sigmoid(logits)
        error = probs - y
        grad_w = (Xs.T @ error) / len(Xs) + (l2 * weights)
        grad_b = float(error.mean())
        weights -= learning_rate * grad_w
        bias -= learning_rate * grad_b

    return ModelSummary(mean=mean, std=std, weights=weights, bias=bias)


def predict_home_win(model: ModelSummary, row: dict[str, float | int | str]) -> float:
    features = np.array([float(row[col]) for col in FEATURE_COLUMNS], dtype=float)
    scaled = (features - model.mean) / model.std
    return float(sigmoid(scaled @ model.weights + model.bias))


def describe_phase(phase: str) -> str:
    descriptions = {
        "clutch": "final cerrado, mercado normalmente mas eficiente",
        "coin_flip": "partido parejo, pocos detalles deciden",
        "momentum": "hay racha reciente y el mercado puede sobrerreaccionar",
        "control": "un equipo controla, cuidado con cuotas demasiado obvias",
        "normal": "partido estable",
        "garbage": "tiempo basura, mercado poco util",
    }
    return descriptions.get(phase, phase)


def build_live_row(
    minute: float,
    home_score: float,
    away_score: float,
    home_odds: float,
    away_odds: float,
    pregame_edge: float,
    recent_run: float,
    foul_pressure: float,
) -> dict[str, float | int | str]:
    minute = clamp(float(minute), 1.0, 48.0)
    time_remaining = max(0.0, 48.0 - minute)
    score_diff = float(home_score - away_score)
    total_points = float(home_score + away_score)
    clutch_time = int(time_remaining <= 6.0 and abs(score_diff) <= 8.0)
    garbage_time = int(time_remaining <= 8.0 and abs(score_diff) >= 15.0)
    phase = infer_phase(int(round(minute)), score_diff, recent_run, clutch_time, garbage_time)
    return {
        "game_id": -1.0,
        "minute": float(minute),
        "time_remaining": float(time_remaining),
        "home_score": float(home_score),
        "away_score": float(away_score),
        "score_diff": score_diff,
        "abs_score_diff": abs(score_diff),
        "pregame_edge": float(pregame_edge),
        "recent_run": float(recent_run),
        "pace_index": total_points / max(1.0, minute),
        "home_points_per_min": float(home_score) / max(1.0, minute),
        "away_points_per_min": float(away_score) / max(1.0, minute),
        "foul_pressure": clamp(float(foul_pressure), 0.55, 1.85),
        "clutch_time": clutch_time,
        "garbage_time": garbage_time,
        "comeback_pressure": max(0.0, -score_diff) / max(4.0, time_remaining + 3.0),
        "phase": phase,
        "home_odds": float(home_odds),
        "away_odds": float(away_odds),
        "home_win": 0.0,
    }


def prompt_float(label: str, default: float) -> float:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return float(default)
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return float(default)


def collect_manual_snapshot() -> dict[str, float | int | str]:
    print("\nCARGA MANUAL BASKETBALL LIVE")
    print("Pregame edge = ventaja prepartido del local en puntos, si la conoces.")
    print("Recent run = parcial reciente a favor del local en los ultimos 2-3 minutos.")
    print("Foul pressure = 1.0 normal, 1.2 mas faltas/bonus, 0.8 juego limpio.")

    minute = prompt_float("Minuto", 24.0)
    home_score = prompt_float("Puntos local", 58.0)
    away_score = prompt_float("Puntos visitante", 54.0)
    home_odds = prompt_float("Cuota local", 1.80)
    away_odds = prompt_float("Cuota visitante", 2.00)
    pregame_edge = prompt_float("Ventaja prepartido local", 0.0)
    recent_run = prompt_float("Parcial reciente local", 0.0)
    foul_pressure = prompt_float("Presion de faltas", 1.0)
    return build_live_row(
        minute=minute,
        home_score=home_score,
        away_score=away_score,
        home_odds=home_odds,
        away_odds=away_odds,
        pregame_edge=pregame_edge,
        recent_run=recent_run,
        foul_pressure=foul_pressure,
    )


def recommend_candidate(model: ModelSummary, row: dict[str, float | int | str], config: BacktestConfig) -> LiveDecision | None:
    minute = int(row["minute"])
    if minute < config.min_minute or minute > config.max_minute:
        return None

    prob_home = predict_home_win(model, row)
    prob_away = 1.0 - prob_home
    home_odds = float(row["home_odds"])
    away_odds = float(row["away_odds"])
    implied_home = 1.0 / home_odds
    implied_away = 1.0 / away_odds
    edge_home = prob_home - implied_home
    edge_away = prob_away - implied_away

    if edge_home >= edge_away:
        side = "HOME"
        edge = edge_home
        odds = home_odds
        prob_model = prob_home
    else:
        side = "AWAY"
        edge = edge_away
        odds = away_odds
        prob_model = prob_away

    if edge < config.edge_threshold:
        return None

    phase = str(row["phase"])
    if int(row["garbage_time"]) == 1:
        return None
    if phase in {"clutch", "coin_flip", "control"}:
        return None
    if odds > 2.05:
        return None

    kelly = max(0.0, ((odds * prob_model) - 1.0) / max(0.01, odds - 1.0))
    stake_fraction = min(config.max_stake_fraction, kelly * config.kelly_fraction)
    if stake_fraction < config.min_stake_fraction:
        return None

    quality = edge * stake_fraction * (1.04 if int(row["clutch_time"]) == 1 else 1.0)
    return LiveDecision(
        side=side,
        edge=edge,
        odds=odds,
        prob_model=prob_model,
        prob_market=implied_home if side == "HOME" else implied_away,
        stake_fraction=stake_fraction,
        quality=quality,
        phase=str(row["phase"]),
        minute=float(row["minute"]),
    )


def evaluate_candidate(model: ModelSummary, row: dict[str, float | int | str], config: BacktestConfig) -> dict[str, float | str] | None:
    recommendation = recommend_candidate(model, row, config)
    if recommendation is None:
        return None
    return {
        "side": recommendation.side,
        "edge": recommendation.edge,
        "odds": recommendation.odds,
        "prob_model": recommendation.prob_model,
        "stake_fraction": recommendation.stake_fraction,
        "quality": recommendation.quality,
        "phase": recommendation.phase,
        "minute": recommendation.minute,
        "home_win": float(row["home_win"]),
        "game_id": float(row["game_id"]),
    }


def backtest(model: ModelSummary, rows: list[dict[str, float | int | str]], config: BacktestConfig) -> tuple[dict[str, float], list[dict[str, float | str]]]:
    bankroll = config.bankroll_start
    total_staked = 0.0
    total_profit = 0.0
    grouped: dict[int, list[dict[str, float | int | str]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["game_id"])].append(row)

    bets: list[dict[str, float | str]] = []
    for _, group in sorted(grouped.items()):
        best_bet: dict[str, float | str] | None = None
        for row in group:
            candidate = evaluate_candidate(model, row, config)
            if candidate is None:
                continue
            if best_bet is None or float(candidate["quality"]) > float(best_bet["quality"]):
                best_bet = candidate

        if best_bet is None:
            continue

        stake_amount = config.bankroll_start * float(best_bet["stake_fraction"])
        total_staked += stake_amount
        home_win = int(best_bet["home_win"])
        side = str(best_bet["side"])
        odds = float(best_bet["odds"])
        won = (side == "HOME" and home_win == 1) or (side == "AWAY" and home_win == 0)
        profit = stake_amount * (odds - 1.0) if won else -stake_amount
        bankroll += profit
        total_profit += profit

        best_bet["stake_amount"] = stake_amount
        best_bet["profit"] = profit
        best_bet["won"] = float(won)
        best_bet["bankroll_after"] = bankroll
        bets.append(best_bet)

    if not bets:
        return {
            "bankroll_start": config.bankroll_start,
            "bankroll_end": bankroll,
            "bets": 0,
            "profit": 0.0,
            "roi": 0.0,
            "hit_rate": 0.0,
        }, []

    hit_rate = sum(float(bet["won"]) for bet in bets) / len(bets)
    summary = {
        "bankroll_start": config.bankroll_start,
        "bankroll_end": bankroll,
        "bets": float(len(bets)),
        "profit": total_profit,
        "roi": total_profit / total_staked if total_staked > 0 else 0.0,
        "hit_rate": hit_rate,
    }
    return summary, bets


def summarise_by_phase(bets: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"bets": 0.0, "wins": 0.0, "profit": 0.0, "stake": 0.0})
    for bet in bets:
        phase = str(bet["phase"])
        grouped[phase]["bets"] += 1.0
        grouped[phase]["wins"] += float(bet["won"])
        grouped[phase]["profit"] += float(bet["profit"])
        grouped[phase]["stake"] += float(bet["stake_amount"])

    rows: list[dict[str, float | str]] = []
    for phase, data in grouped.items():
        roi = data["profit"] / data["stake"] if data["stake"] > 0 else 0.0
        hit_rate = data["wins"] / data["bets"] if data["bets"] > 0 else 0.0
        rows.append({"phase": phase, "bets": data["bets"], "hit_rate": hit_rate, "roi": roi})
    rows.sort(key=lambda row: float(row["roi"]), reverse=True)
    return rows


def run_stress_suite(universes: int, games_per_universe: int, seed: int, config: BacktestConfig) -> list[dict[str, float]]:
    results: list[dict[str, float]] = []
    for offset in range(universes):
        rows = generate_synthetic_games(games_per_universe, seed + offset)
        train_rows, test_rows = split_games(rows)
        model = train_model(train_rows)
        summary, _ = backtest(model, test_rows, config)
        summary["seed"] = float(seed + offset)
        results.append(summary)
    return results


def print_phase_table(phase_rows: list[dict[str, float | str]]) -> None:
    if not phase_rows:
        print("No hubo apuestas para resumir por fase.")
        return

    print("\nRendimiento por fase:")
    for row in phase_rows:
        print(
            f"  {str(row['phase']):10s} | bets {int(float(row['bets'])):3d}"
            f" | hit rate {float(row['hit_rate']):.1%} | ROI {float(row['roi']):.1%}"
        )


def stake_label(stake_fraction: float) -> str:
    if stake_fraction >= 0.008:
        return "fuerte"
    if stake_fraction >= 0.005:
        return "suave"
    if stake_fraction > 0:
        return "muy suave"
    return "sin apuesta"


def print_manual_recommendation(row: dict[str, float | int | str], decision: LiveDecision | None) -> None:
    print("\n" + "=" * 72)
    print("BASKETBALL LIVE CHECK")
    print("=" * 72)
    print(
        f"  Min {float(row['minute']):.1f} | marcador {int(float(row['home_score']))}-{int(float(row['away_score']))}"
        f" | fase {row['phase']}"
    )
    print(f"  Lectura de fase: {describe_phase(str(row['phase']))}.")
    print(
        f"  Mercado: local {float(row['home_odds']):.2f} | visitante {float(row['away_odds']):.2f}"
    )

    if decision is None:
        print("  Decision: PASAR")
        print("  Motivo: no veo ventaja suficiente o el tipo de partido es demasiado eficiente para entrar.")
        print("=" * 72)
        return

    print(f"  Decision: {decision.side}")
    print(
        f"  Prob modelo {decision.prob_model:.1%} | prob mercado {decision.prob_market:.1%}"
        f" | edge {decision.edge:.1%}"
    )
    print(f"  Entrada: {stake_label(decision.stake_fraction)} ({decision.stake_fraction:.1%} de banca base)")
    if str(row["phase"]) in {"clutch", "coin_flip"}:
        print("  Motivo: solo entraria si el edge supera claramente al mercado, porque el cierre suele estar muy bien ajustado.")
    elif str(row["phase"]) == "momentum":
        print("  Motivo: hay racha reciente y el mercado puede estar sobrerreaccionando.")
    elif str(row["phase"]) == "control":
        print("  Motivo: hay control de un lado, pero solo entro si la cuota aun deja valor real.")
    else:
        print("  Motivo: partido relativamente estable con una diferencia modelada frente al mercado.")
    print("=" * 72)


def print_single_backtest(summary: dict[str, float], bets: list[dict[str, float | str]]) -> None:
    print("\n" + "=" * 72)
    print("BASKETBALL LIVE MODEL")
    print("=" * 72)
    print(f"  Bankroll inicial: {summary['bankroll_start']:.2f}")
    print(f"  Bankroll final:   {summary['bankroll_end']:.2f}")
    print(f"  Ganancia:         {summary['profit']:.2f}")
    print(f"  Apuestas:         {int(summary['bets'])}")
    print(f"  Hit rate:         {summary['hit_rate']:.1%}")
    print(f"  ROI:              {summary['roi']:.1%}")
    if summary["roi"] > 0.40:
        print("  Aviso: este ROI sintetico es demasiado alto; tomalo como senal de sobreajuste del entorno.")
    if bets:
        avg_minute = sum(float(bet["minute"]) for bet in bets) / len(bets)
        print(f"  Minuto medio:     {avg_minute:.1f}")
    print_phase_table(summarise_by_phase(bets))
    print("=" * 72)


def print_stress_report(stress_rows: list[dict[str, float]]) -> None:
    roi_values = [row["roi"] for row in stress_rows]
    profit_values = [row["profit"] for row in stress_rows]
    hit_rates = [row["hit_rate"] for row in stress_rows]
    bet_counts = [row["bets"] for row in stress_rows]

    print("\n" + "=" * 72)
    print("STRESS TEST BASKETBALL LIVE")
    print("=" * 72)
    print("  Nota: esto sigue siendo sintetico. Sirve para castigar ideas malas rapido,")
    print("  pero no sustituye un backtest con snapshots reales del mercado live.")
    print(f"  Universos probados: {len(stress_rows)}")
    print(f"  ROI medio:          {np.mean(roi_values):.1%}")
    print(f"  ROI minimo:         {np.min(roi_values):.1%}")
    print(f"  ROI maximo:         {np.max(roi_values):.1%}")
    print(f"  Ganancia media:     {np.mean(profit_values):.2f}")
    print(f"  Hit rate medio:     {np.mean(hit_rates):.1%}")
    print(f"  Apuestas medias:    {np.mean(bet_counts):.1f}")
    if np.mean(roi_values) > 0.40:
        print("  Aviso: el entorno sintetico sigue demasiado favorable; esto no demuestra edge real de mercado.")
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modelo live de basketball con backtest y stress test sintetico.")
    parser.add_argument("--games", type=int, default=900, help="Juegos sinteticos para el backtest principal.")
    parser.add_argument("--train-games", type=int, default=1400, help="Juegos sinteticos para entrenar en modo manual.")
    parser.add_argument("--seed", type=int, default=7, help="Seed reproducible.")
    parser.add_argument("--stress", action="store_true", help="Corre varias semillas y resume estabilidad.")
    parser.add_argument("--manual", action="store_true", help="Permite cargar un partido live manualmente.")
    parser.add_argument("--universes", type=int, default=8, help="Numero de universos sinteticos para stress test.")
    parser.add_argument("--stress-games", type=int, default=700, help="Juegos por universo en stress test.")
    parser.add_argument("--edge-threshold", type=float, default=0.05, help="Edge minimo para apostar.")
    parser.add_argument("--max-stake", type=float, default=0.008, help="Stake maximo como fraccion de banca.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = BacktestConfig(
        edge_threshold=args.edge_threshold,
        max_stake_fraction=args.max_stake,
    )

    if args.manual:
        train_rows = generate_synthetic_games(args.train_games, args.seed)
        model = train_model(train_rows)
        row = collect_manual_snapshot()
        decision = recommend_candidate(model, row, config)
        print_manual_recommendation(row, decision)
        return

    rows = generate_synthetic_games(args.games, args.seed)
    train_rows, test_rows = split_games(rows)
    model = train_model(train_rows)
    summary, bets = backtest(model, test_rows, config)
    print_single_backtest(summary, bets)

    if args.stress:
        stress_rows = run_stress_suite(args.universes, args.stress_games, args.seed + 100, config)
        print_stress_report(stress_rows)


if __name__ == "__main__":
    main()
